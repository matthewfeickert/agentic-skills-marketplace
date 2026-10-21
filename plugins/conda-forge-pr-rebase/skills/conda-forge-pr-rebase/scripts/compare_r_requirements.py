"""Compare an R feedstock recipe's host/run requirements with the package DESCRIPTION.

Usage (from the feedstock root):
    pixi exec -s python -s pyyaml -- python compare_r_requirements.py [recipe/recipe.yaml | recipe/meta.yaml]

Handles v1 recipe.yaml and, approximately, v0 meta.yaml (the format of most
CRAN-generated R feedstocks): {% set %} variables and {{ }} expressions are
substituted, and list items carrying a "# [selector]" are set aside as
conditional and reported for review instead of being compared.

Downloads the source tarball from the recipe's rendered source.url (trying each
URL if it is a list, e.g. a CRAN mirror plus the CRAN Archive), checks its
sha256, and compares DESCRIPTION's Depends/Imports/LinkingTo with the recipe.
Following conda-forge R conventions:
  - run  <- Depends + Imports
  - host <- Depends + Imports + LinkingTo
  - Depends: R (>= x) <-> r-base >=x
Base R packages (utils, tools, stats, ...) are not conda packages and are skipped.
Only r-* entries of the recipe are compared; compilers, make, etc. are ignored.
Exits 1 on any mismatch.
"""

import hashlib
import io
import re
import sys
import tarfile
import urllib.request

import yaml

# Packages that ship with R itself; never separate conda-forge packages.
BASE_R = {
    "base", "compiler", "datasets", "graphics", "grDevices", "grid", "methods",
    "parallel", "splines", "stats", "stats4", "tcltk", "tools", "utils",
}


# conda-forge global variables that recipes use without defining them.
GLOBAL_CONTEXT = {"cran_mirror": "https://cloud.r-project.org"}


def render(value, context):
    context = {**GLOBAL_CONTEXT, **context}
    # Same subset as compare_requirements.py: var, var|lower, var|upper, var[N].
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


def load_recipe(path):
    text = open(path).read()
    if not path.endswith("meta.yaml"):
        return yaml.safe_load(text), []
    context = dict(re.findall(r"{%-?\s*set\s+(\w+)\s*=\s*['\"]([^'\"]*)['\"]\s*-?%}", text))
    conditional, lines = [], []
    for line in text.splitlines():
        if re.match(r"\s*{%.*%}\s*$", line):
            continue
        selector = re.search(r"#\s*\[(.+?)\]\s*$", line)
        line = re.sub(r"{{\s*(\w+)\s*(?:\[\s*(-?\d+)\s*\])?\s*(?:\|\s*(lower|upper)\s*)?}}",
                      lambda m: render("${{ %s%s%s }}" % (m[1], f"[{m[2]}]" if m[2] else "", f"|{m[3]}" if m[3] else ""), context), line)
        line = re.sub(r"{{.*?}}", "JINJA", line)  # compiler('c'), posix, cran_mirror without a set, ...
        if selector and re.match(r"\s*-\s", line):
            conditional.append(f"{line.split('#')[0].strip()}  # [{selector[1]}]")
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    recipe = yaml.safe_load("\n".join(lines))
    recipe.setdefault("context", {}).update(context)
    return recipe, conditional


def parse_deps(field):
    """'pkg (>= 1.0), other' -> {'r-pkg': '>=1.0', 'r-other': ''} plus the R bound."""
    deps, r_bound = {}, ""
    for item in filter(None, (x.strip() for x in field.split(","))):
        m = re.match(r"([A-Za-z0-9.]+)\s*(?:\(\s*([<>=!]+)\s*([^)\s]+)\s*\))?", item)
        name, op, ver = m[1], m[2], m[3]
        spec = f"{op}{ver}" if op else ""
        if name == "R":
            r_bound = spec
        elif name not in BASE_R:
            deps["r-" + name.lower()] = spec
    return deps, r_bound


def recipe_r_reqs(entries, context):
    reqs, conditional = {}, []
    for item in entries or []:
        if not isinstance(item, str):
            conditional.append(item)  # if/then blocks
            continue
        pkg, _, spec = render(item, context).partition(" ")
        if pkg.startswith("r-") or pkg == "r-base":
            reqs[pkg] = spec.replace(" ", "")
    return reqs, conditional


def compare(label, recipe, expected):
    only_recipe = sorted(set(recipe) - set(expected))
    only_desc = sorted(set(expected) - set(recipe))
    bounds = {k: {"recipe": recipe[k], "DESCRIPTION": expected[k]}
              for k in recipe.keys() & expected.keys() if recipe[k] != expected[k]}
    print(f"--- {label}")
    print(f"only in recipe: {only_recipe}")
    print(f"only in DESCRIPTION: {only_desc}")
    print(f"bound mismatches: {bounds}")
    return not (only_recipe or only_desc or bounds)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "recipe/recipe.yaml"
    recipe, selector_lines = load_recipe(path)
    context = recipe.get("context", {})
    source = recipe["source"]
    urls = source["url"] if isinstance(source["url"], list) else [source["url"]]
    urls = [render(u, context) for u in urls]

    data, errors = None, []
    for url in urls:
        try:
            with urllib.request.urlopen(url) as r:
                data = r.read()
            print(f"source: {url}")
            break
        except Exception as e:  # try the next mirror / Archive URL
            errors.append(f"{url}: {e}")
    if data is None:
        sys.exit("could not download source:\n  " + "\n  ".join(errors))

    recipe_sha = render(str(source.get("sha256", "")), context)
    sha_ok = hashlib.sha256(data).hexdigest() == recipe_sha
    print(f"recipe sha256 matches source: {sha_ok}")

    tar = tarfile.open(fileobj=io.BytesIO(data))
    desc_member = min((m for m in tar.getmembers() if m.name.endswith("/DESCRIPTION") and m.name.count("/") == 1),
                      key=lambda m: len(m.name))
    top = desc_member.name.split("/")[0]
    desc = tar.extractfile(desc_member).read().decode()
    # DCF: continuation lines start with whitespace.
    fields = dict(re.findall(r"^([\w/@.-]+):[ \t]*(.*(?:\n[ \t].*)*)", desc, re.M))
    print(f"DESCRIPTION Version: {fields.get('Version', '?').strip()}")

    depends, r_bound = parse_deps(fields.get("Depends", ""))
    imports, _ = parse_deps(fields.get("Imports", ""))
    linking, _ = parse_deps(fields.get("LinkingTo", ""))
    run_expected = {**depends, **imports, "r-base": r_bound}
    host_expected = {**run_expected, **linking}
    for k, v in linking.items():  # LinkingTo bounds only constrain host
        host_expected[k] = v or host_expected.get(k, "")

    run_reqs, run_cond = recipe_r_reqs(recipe["requirements"].get("run"), context)
    host_reqs, host_cond = recipe_r_reqs(recipe["requirements"].get("host"), context)
    # r-base is conventionally unpinned in recipes (the variant pins it); only flag
    # it if DESCRIPTION requires an R newer than any bound the recipe declares.
    if r_bound and any("r-base" in reqs and not reqs["r-base"] for reqs in (run_reqs, host_reqs)):
        print(f"REVIEW: DESCRIPTION requires R {r_bound}; recipe r-base is unpinned "
              "(fine if every r_base variant in .ci_support satisfies it)")
    for reqs in (run_reqs, host_reqs):
        if "r-base" in reqs and not reqs["r-base"] and r_bound:
            reqs["r-base"] = r_bound

    run_ok = compare("run requirements vs Depends + Imports", run_reqs, run_expected)
    host_ok = compare("host requirements vs Depends + Imports + LinkingTo", host_reqs, host_expected)
    # Only R package entries matter here (e.g. "r-later  # [build_platform != target_platform]"
    # in build for cross-compilation); compilers/tools behind selectors are irrelevant.
    r_selector_lines = [l for l in selector_lines if re.match(r"-\s+(cross-)?r-", l)]
    if run_cond or host_cond or r_selector_lines:
        print(f"REVIEW conditional recipe entries (not compared): {run_cond + host_cond + r_selector_lines}")

    # Compiled code needs compilers in build and must not be noarch.
    has_src = any(m.name.startswith(f"{top}/src/") for m in tar.getmembers())
    noarch = recipe.get("build", {}).get("noarch")
    print(f"package has src/: {has_src} | recipe noarch: {noarch}")
    arch_ok = not (has_src and noarch)
    if not arch_ok:
        print("MISMATCH: package has compiled code (src/) but the recipe is noarch")
    if linking and not has_src:
        print("REVIEW: LinkingTo without src/ is unusual; check the package layout")
    if "SystemRequirements" in fields:
        print(f"REVIEW SystemRequirements: {' '.join(fields['SystemRequirements'].split())}")

    ok = sha_ok and run_ok and host_ok and arch_ok
    print("RESULT:", "MATCH" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
