"""Compare a feedstock recipe's run and host requirements with the sdist's metadata.

Usage (from the feedstock root):
    pixi exec -s python -s pyyaml -s packaging -- python compare_requirements.py [recipe/recipe.yaml] [--pypi-name NAME]

Downloads the sdist for the recipe's version from PyPI, reads Requires-Dist and
Requires-Python from its PKG-INFO and [build-system].requires from its
pyproject.toml, and reports any differences from requirements.run and
requirements.host. Exits 0 on a match, 1 on any mismatch, and 2 if the
script fails to run.
"""

import argparse
import io
import json
import re
import sys
import tarfile
import tomllib
import traceback

import yaml
from packaging.requirements import Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.version import Version
from packaging.utils import canonicalize_name

from download import fetch

# PyPI name -> conda-forge name, where they differ by convention.
# Extend as needed; unknown differences are reported for manual review.
# Build requirements that conda-forge recipes conventionally omit from host.
HOST_OPTIONAL = {"wheel"}
# Dependencies that a conda-forge package fully controls, so recipes list only
# the controlling package: conda-forge's jax always pins the jaxlib it needs.
IMPLIED_BY = {"jaxlib": "jax"}
PYPI_TO_CONDA = {
    "matplotlib": "matplotlib-base",
    "torch": "pytorch",
    "tables": "pytables",
    "graphviz": "python-graphviz",
}


def render(value, context):
    # Handles the common recipe expressions: var, var|lower, var|upper, var[N].
    # Anything else is left unrendered so a mismatch surfaces instead of a guess.
    def repl(m):
        var, index, filt = m[1], m[2], m[3]
        if var not in context:
            return m[0]
        out = str(context[var])
        if index is not None:
            out = out[int(index)]
        if filt == "lower":
            out = out.lower()
        elif filt == "upper":
            out = out.upper()
        return out

    return re.sub(r"\$\{\{\s*(\w+)\s*(?:\[\s*(-?\d+)\s*\])?\s*(?:\|\s*(lower|upper)\s*)?\}\}", repl, value)


def recipe_reqs(entries, context, skip):
    reqs, conditional = {}, []
    for item in entries or []:
        if not isinstance(item, str):
            conditional.append(item)  # if/then blocks
            continue
        pkg, _, spec = render(item, context).partition(" ")
        if pkg in skip:
            continue
        reqs[canonicalize_name(pkg)] = spec.replace(" ", "")
    return reqs, conditional


def clauses(spec):
    # Compare bounds semantically: Specifier equality canonicalizes versions
    # (>=1.27.0 == >=1.27), ~=X.Y.Z is expanded to >=X.Y.Z,<X.(Y+1), and an
    # upper bound written as <N.dev0 (how the conda-forge bot spells ~=) is
    # treated as <N.
    try:
        specs = SpecifierSet(spec)
    except Exception:  # not PEP 440 (e.g. conda-only syntax); fall back to text
        return set(spec.split(",")) - {""}
    out = set()
    for s in specs:
        v = Version(s.version) if s.operator not in ("==", "!=") or "*" not in s.version else None
        if s.operator == "~=":
            upper = list(v.release[:-1])
            upper[-1] += 1
            out |= {Specifier(f">={v}"), Specifier("<" + ".".join(map(str, upper)))}
        elif s.operator == "<" and v is not None and v.dev is not None and v.pre is None:
            out.add(Specifier(f"<{v.base_version}"))
        else:
            out.add(s)
    return out


def diff_bounds(recipe, sdist):
    return {
        k: {"recipe": recipe[k], "sdist": sdist[k]}
        for k in recipe.keys() & sdist.keys()
        if clauses(recipe[k]) != clauses(sdist[k])
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", nargs="?", default="recipe/recipe.yaml")
    parser.add_argument("--pypi-name", help="PyPI project name, if it differs from package.name")
    args = parser.parse_args()

    recipe = yaml.safe_load(open(args.recipe))
    context = recipe.get("context", {})
    name = args.pypi_name or render(recipe["package"]["name"], context)
    version = render(str(context.get("version", recipe["package"]["version"])), context)

    urls = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))["urls"]
    sdist = next(u for u in urls if u["packagetype"] == "sdist")
    print(f"sdist: {sdist['filename']} sha256={sdist['digests']['sha256']}")
    recipe_sha = render(str(recipe["source"].get("sha256", "")), context)
    sha_ok = recipe_sha == sdist["digests"]["sha256"]
    print(f"recipe sha256 matches PyPI: {sha_ok}")

    # source.url must stay version-agnostic. files.pythonhosted.org paths embed
    # a per-file blake2b hash (packages/ab/cd/<60 hex>/...), so templating the
    # version into one breaks the next version update.
    recipe_url = render(str(recipe["source"].get("url", "")), context)
    hashed_url = bool(re.search(r"files\.pythonhosted\.org/packages/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/", recipe_url))
    print(f"recipe source.url: {recipe_url}")
    if hashed_url:
        print("MISMATCH: source.url is a hash-based files.pythonhosted.org path; "
              f"use https://pypi.org/packages/source/{name[0]}/{name}/<sdist-filename with ${{{{ version }}}}>")

    tar = tarfile.open(fileobj=io.BytesIO(fetch(sdist["url"])))
    top_level = {m.name.split("/", 1)[1]: m for m in tar.getmembers() if m.name.count("/") == 1}
    lines = tar.extractfile(top_level["PKG-INFO"]).read().decode().splitlines()

    requires_python = next((l.split(":", 1)[1].strip() for l in lines if l.startswith("Requires-Python:")), None)
    sdist_reqs, marker_reqs = {}, []
    for line in lines:
        if not line.startswith("Requires-Dist:"):
            continue
        req = Requirement(line.split(":", 1)[1].strip())
        if req.marker and "extra" in str(req.marker):
            continue
        if req.marker:
            # Needs a human decision: e.g. drop tomli; python_version < '3.11' when
            # python_min >= 3.11, or keep argcomplete; platform_system != 'Windows'
            # unconditional in a noarch recipe.
            marker_reqs.append(str(req))
            if "python_version" in str(req.marker) or "python_full_version" in str(req.marker):
                continue
        cname = PYPI_TO_CONDA.get(canonicalize_name(req.name), canonicalize_name(req.name))
        sdist_reqs[cname] = str(req.specifier)

    run_reqs, skipped = recipe_reqs(recipe["requirements"].get("run"), context, {"python"})

    # Implied dependencies are never required in the recipe. An upstream floor on
    # one is reported against its controlling package (e.g. raise jax, not jaxlib).
    implied_notes = []
    for dep, parent in IMPLIED_BY.items():
        if dep in sdist_reqs and parent in sdist_reqs:
            spec = sdist_reqs.pop(dep)
            implied_notes.append(f"{dep}{spec} (sdist) is provided by conda-forge's {parent}"
                                 + (f"; make sure the {parent} floor covers it" if spec else ""))
        if dep in run_reqs and parent in run_reqs:
            run_reqs.pop(dep)
            implied_notes.append(f"recipe lists {dep}, which is redundant with {parent} (leave existing entries unless asked)")

    python_min = context.get("python_min")
    print(f"Requires-Python: {requires_python} | recipe python_min: {python_min}")
    only_recipe = sorted(set(run_reqs) - set(sdist_reqs))
    only_sdist = sorted(set(sdist_reqs) - set(run_reqs))
    bounds = diff_bounds(run_reqs, sdist_reqs)
    print("--- run requirements vs Requires-Dist")
    print(f"only in recipe: {only_recipe}")
    print(f"only in sdist:  {only_sdist}")
    print(f"bound mismatches: {bounds}")
    for note in implied_notes:
        print(f"info: {note}")
    if marker_reqs:
        print(f"REVIEW marker-conditional sdist requirements: {marker_reqs}")
    if skipped:
        print(f"REVIEW conditional recipe run entries (not compared): {skipped}")

    # Host requirements vs [build-system].requires. Extra host entries (pip,
    # backend plugins pulled in transitively upstream) are informational only.
    print("--- host requirements vs [build-system].requires")
    host_ok = True
    if "pyproject.toml" not in top_level:
        print("no pyproject.toml in sdist; REVIEW setup.py/setup.cfg build requirements by hand")
    else:
        pyproject = tomllib.loads(tar.extractfile(top_level["pyproject.toml"]).read().decode())
        build_system = pyproject.get("build-system", {})
        print(f"build-backend: {build_system.get('build-backend')}")
        build_reqs, build_extras = {}, []
        for r in build_system.get("requires", []):
            req = Requirement(r)
            if req.extras:
                # e.g. versioneer[toml]: the conda package may not pull in the extra's deps
                build_extras.append(str(req))
            cname = PYPI_TO_CONDA.get(canonicalize_name(req.name), canonicalize_name(req.name))
            build_reqs[cname] = str(req.specifier)
        host_reqs, host_skipped = recipe_reqs(recipe["requirements"].get("host"), context, {"python"})
        host_missing = sorted(set(build_reqs) - set(host_reqs) - HOST_OPTIONAL)
        # Host version bounds are avoided on conda-forge, so upstream bounds that
        # the host entries lack are informational only. Only a missing or renamed
        # build tool (e.g. versioneer vs. versioneer-518) is a mismatch.
        host_unbounded = {}
        for k in host_reqs.keys() & build_reqs.keys():
            missing = sorted(str(c) for c in clauses(build_reqs[k]) - clauses(host_reqs[k]))
            if missing:
                host_unbounded[k] = missing
        print(f"build-system.requires: {build_reqs}")
        print(f"missing from host: {host_missing}")
        if host_unbounded:
            print(f"info: upstream build-tool bounds not in host (do not add): {host_unbounded}")
        print(f"only in host (informational): {sorted(set(host_reqs) - set(build_reqs))}")
        if build_extras:
            print(f"REVIEW build requirements with extras (add the extra's deps to host if needed): {build_extras}")
        if host_skipped:
            print(f"REVIEW conditional recipe host entries (not compared): {host_skipped}")
        host_ok = not host_missing

    python_ok = requires_python is None or python_min is None or requires_python.replace(" ", "") == f">={python_min}"
    if not python_ok:
        print("MISMATCH: Requires-Python does not match python_min")
    ok = sha_ok and not hashed_url and python_ok and host_ok and not (only_recipe or only_sdist or bounds)
    print("RESULT:", "MATCH" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Exit 2, not 1, so a script that could not run (download failure,
        # unsupported recipe layout, ...) is never read as a requirements mismatch.
        traceback.print_exc()
        print("RESULT: ERROR (the script failed; this is not a requirements mismatch)")
        sys.exit(2)
