"""Shared helpers for comparing compiled-language (Rust, Go) recipes with their source.

Unlike Python and R packages, Rust and Go packages have no released metadata
that lists conda requirements. These helpers instead compare the PR's source
with the source of the version on the base branch (lock files, toolchain
versions, binaries, licenses), and resolve the paths that the recipe's build
script uses.

Only v1 recipe.yaml recipes with a single output are supported. The build
script is simulated for linux-64: `if:` conditions are evaluated for that
platform, `cd`/`pushd`/`popd` are followed, and the commands are parsed into
tokens. Nothing is ever executed.
"""

import argparse
import ast
import fnmatch
import hashlib
import io
import json
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from download import fetch

# The platform the build script is simulated for. Conditions that can't be
# evaluated with these names include both branches.
LINUX_64 = {
    "unix": True, "linux": True, "osx": False, "win": False,
    "x86_64": True, "x86": False, "aarch64": False, "arm64": False, "ppc64le": False,
    "linux64": True, "build_platform": "linux-64", "target_platform": "linux-64",
    "host_platform": "linux-64",
}
EXPR = re.compile(r"\$\{\{\s*(\w+)\s*(?:\[\s*(-?\d+)\s*\])?\s*(?:\|\s*(lower|upper)\s*)?\}\}")


class Report:
    """Print findings as they are made and remember whether any was a mismatch."""

    def __init__(self):
        self.mismatch = False

    def section(self, title):
        print(f"--- {title}")

    def info(self, msg):
        print(f"info: {msg}")

    def review(self, msg):
        print(f"REVIEW: {msg}")

    def fail(self, msg):
        self.mismatch = True
        print(f"MISMATCH: {msg}")

    def finish(self, work):
        for source in work.uninspected:
            self.review(f"additional source not inspected: {source}")
        print("RESULT:", "MISMATCH" if self.mismatch else "MATCH")
        return 1 if self.mismatch else 0


def render(value, context):
    # Same subset as compare_requirements.py: var, var|lower, var|upper, var[N].
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

    return EXPR.sub(repl, str(value))


def condition(expr):
    """Evaluate a recipe `if:` condition for linux-64, or None if it can't be.

    Only names from LINUX_64, string constants, and/or/not, == and != are
    understood, so recipe text is never executed.
    """

    def value(node):
        if isinstance(node, ast.BoolOp):
            values = [value(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not value(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in (ast.Eq, ast.NotEq):
            equal = value(node.left) == value(node.comparators[0])
            return equal if isinstance(node.ops[0], ast.Eq) else not equal
        if isinstance(node, ast.Name) and node.id in LINUX_64:
            return LINUX_64[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool)):
            return node.value
        raise ValueError(f"unsupported condition: {expr}")

    try:
        return bool(value(ast.parse(str(expr), mode="eval").body))
    except (SyntaxError, ValueError):
        return None


def flatten(node):
    """Strings of a recipe list for linux-64, resolving `if:`/`then:`/`else:` blocks."""
    if node is None:
        return []
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for item in node for s in flatten(item)]
    if isinstance(node, dict) and "if" in node:
        taken = condition(node["if"])
        if taken is None:  # unknown condition: keep both branches
            return flatten(node.get("then")) + flatten(node.get("else"))
        return flatten(node.get("then") if taken else node.get("else"))
    return []


class Recipe:
    def __init__(self, text, path, base_ref=None):
        self.path = path
        self.base_ref = base_ref  # read build.sh from this git ref instead of disk
        if path.endswith("meta.yaml"):
            raise RuntimeError("v0 meta.yaml recipes are not supported; compare by hand")
        self.data = yaml.safe_load(text)
        if "outputs" in self.data:
            raise RuntimeError("multi-output recipes are not supported; compare each output by hand")
        self.context = {}
        for key, value in (self.data.get("context") or {}).items():
            self.context[key] = render(value, self.context)
        self.version = self.context.get("version") or render(self.data["package"]["version"], self.context)

    def requirements(self, kind):
        """Package names in requirements.<kind>, with compiler('x') as 'compiler:x'."""
        names = []
        for entry in flatten((self.data.get("requirements") or {}).get(kind)):
            m = re.search(r"compiler\(\s*['\"]([\w-]+)['\"]\s*\)", entry)
            names.append(f"compiler:{m[1]}" if m else render(entry, self.context).split()[0])
        return names

    def script_env(self):
        script = (self.data.get("build") or {}).get("script")
        return script.get("env", {}) if isinstance(script, dict) else {}

    def script_lines(self):
        """The build script's lines for linux-64 (from recipe.yaml or recipe/build.sh)."""
        script = (self.data.get("build") or {}).get("script")
        if isinstance(script, dict):
            script = script.get("content", script.get("file"))
        lines = flatten(script)
        # A missing script means recipe/build.sh, and a single word may name a
        # script file next to the recipe (e.g. `script: build_rust.sh`).
        if not lines or (len(lines) == 1 and re.fullmatch(r"[\w.-]+", lines[0])):
            name = lines[0] if lines else "build"
            text = self.recipe_file(name if name.endswith(".sh") else f"{name}.sh")
            if text is not None:
                lines = [text]
        # Join shell line continuations before splitting into lines.
        text = "\n".join(lines).replace("\\\n", " ")
        return [render(line, self.context) for line in text.splitlines()]

    def recipe_file(self, name):
        rel = posixpath.join(posixpath.dirname(self.path), name)
        if self.base_ref:
            out = subprocess.run(["git", "show", f"{self.base_ref}:{rel}"], check=False, capture_output=True, text=True)
            return out.stdout if out.returncode == 0 else None
        p = Path(rel)
        return p.read_text() if p.exists() else None

    def sources(self):
        source = self.data.get("source") or []
        return source if isinstance(source, list) else [source]

    def main_source(self):
        """The source that carries the version: the first url source that uses it."""
        url_sources = [s for s in self.sources() if isinstance(s, dict) and "url" in s]
        if not url_sources:
            raise RuntimeError("no url source in the recipe (git and path sources are not supported)")
        for s in url_sources:
            urls = s["url"] if isinstance(s["url"], list) else [s["url"]]
            if any("version" in u for u in urls):
                return s
        return url_sources[0]

    def tests(self):
        return self.data.get("tests") or []

    def license_files(self):
        return [render(f, self.context) for f in flatten((self.data.get("about") or {}).get("license_file"))]


def load_recipe(path, base_ref=None):
    """The recipe at path, or at path in git ref base_ref (None if it doesn't exist there)."""
    if base_ref is None:
        return Recipe(Path(path).read_text(), path)
    out = subprocess.run(["git", "show", f"{base_ref}:{path}"], check=False, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return Recipe(out.stdout, path, base_ref=base_ref)


def read_archive(data):
    """{path: bytes} of an archive's regular files, with a single top-level directory
    stripped as rattler-build does (GitHub and crates.io archives have one).

    The archive is read in memory and never extracted to disk, so its contents
    are only ever data.
    """
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}
    else:
        # A single sequential pass ("r|*"): reading members out of order from a
        # compressed tar decompresses the stream from the start again each time.
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|*") as archive:
            files = {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}
    if len({name.split("/")[0] for name in files}) == 1 and all("/" in n for n in files):
        files = {n.split("/", 1)[1]: content for n, content in files.items()}
    return files


class WorkDir:
    """The build's work directory: the main source mounted at its target_directory.

    files maps every path to its contents. Files from the other sources are
    known by name only (they are not downloaded) and map to None.
    """

    def __init__(self, recipe, data):
        main = recipe.main_source()
        self.prefix = resolve(".", render(main.get("target_directory", "."), recipe.context))
        self.files = {resolve(self.prefix, n): content for n, content in read_archive(data).items()}
        self.uninspected = []
        for s in recipe.sources():
            if s is main or not isinstance(s, dict):
                continue
            if "file_name" in s:
                target = render(s.get("target_directory", "."), recipe.context)
                self.files[resolve(target, render(s["file_name"], recipe.context))] = None
            else:
                self.uninspected.append(s.get("url") or s.get("git") or s.get("path"))
        self.dirs = {"."}
        for f in self.files:
            d = posixpath.dirname(f)
            while d and d not in self.dirs:  # an ancestor already present has its own ancestors
                self.dirs.add(d)
                d = posixpath.dirname(d)

    def read(self, path):
        content = self.files[path]
        if content is None:
            raise ValueError(f"{path} comes from a source that is not downloaded")
        return content

    def text(self, path):
        return self.read(path).decode(errors="replace")

    def is_file(self, path):
        return path in self.files

    def is_dir(self, path):
        return posixpath.normpath(path) in self.dirs

    def exists(self, path):
        return self.is_file(path) or self.is_dir(path)

    def glob(self, pattern):
        return [f for f in self.files if fnmatch.fnmatch(f, pattern)]

    def listdir_files(self, path):
        """The main source's files directly in directory path."""
        path = posixpath.normpath(path)
        return [f for f, content in self.files.items()
                if content is not None and (posixpath.dirname(f) or ".") == path]

    def find_up(self, start, name):
        """The nearest path start/name, start/../name, ... that exists, or None."""
        d = start
        while True:
            candidate = resolve(d, name)
            if candidate in self.files:
                return candidate
            if d == ".":
                return None
            d = posixpath.dirname(d) or "."


def fetch_source(recipe):
    """(url, contents) of the recipe's main source, trying each URL in a list."""
    main = recipe.main_source()
    urls = main["url"] if isinstance(main["url"], list) else [main["url"]]
    errors = []
    for url in (render(u, recipe.context) for u in urls):
        try:
            return url, fetch(url)
        except Exception as e:  # try the next URL in the list
            errors.append(f"{url}: {e}")
    raise RuntimeError("could not download source:\n  " + "\n  ".join(errors))


def load_sources(recipe, base_ref, report):
    """The PR's work directory, with its sha256 checked, and the base version's
    recipe and work directory (None when there is nothing to compare against)."""
    base = load_recipe(recipe.path, base_ref)
    compare = base is not None and base.version != recipe.version
    # The downloads are independent, so run them at the same time.
    with ThreadPoolExecutor(max_workers=2) as pool:
        pr_download = pool.submit(fetch_source, recipe)
        base_download = pool.submit(fetch_source, base) if compare else None
        url, data = pr_download.result()
        base_data = base_download.result()[1] if compare else None
    print(f"source: {url}")
    sha, digest = render(recipe.main_source().get("sha256", ""), recipe.context), hashlib.sha256(data).hexdigest()
    if digest == sha:
        print("recipe sha256 matches source: True")
    else:
        report.fail(f"recipe sha256 {sha} does not match the source ({digest})")
    if base is None:
        report.info(f"no recipe at {base_ref}; nothing to compare against")
    elif not compare:
        report.info(f"{base_ref} has the same version {base.version} (rebuild); source unchanged")
    else:
        print(f"base version ({base_ref}): {base.version}")
    return WorkDir(recipe, data), base, WorkDir(base, base_data) if compare else None


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", nargs="?", default="recipe/recipe.yaml")
    parser.add_argument("--base-ref", default="origin/main", help="git ref with the base version's recipe")
    return parser.parse_args()


def run(main):
    """Exit with main()'s code, or 2 if it fails.

    Exit 2, not 1, so a script that could not run (download failure,
    unsupported recipe layout, ...) is never read as a requirements mismatch.
    """
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        print("RESULT: ERROR (the script failed; this is not a requirements mismatch)")
        code = 2
    sys.exit(code)


def print_changes(label, new, old):
    """Print the keys of new that were added, removed or changed relative to old."""
    changed = sorted(f"{n} {old[n]} -> {new[n]}" for n in new.keys() & old.keys() if new[n] != old[n])
    print(f"added {label}: {sorted(new.keys() - old.keys())}")
    print(f"removed {label}: {sorted(old.keys() - new.keys())}")
    print(f"changed {label} ({len(changed)}): {changed}")


def commands(recipe):
    """(cwd, tokens) for each simple command in the build script, simulated for linux-64.

    cwd is relative to the work directory, or None once a `cd` can't be resolved
    (e.g. `cd $SOME_VAR`). Leading VAR=value assignments are dropped, and
    unrendered recipe expressions become the single token $UNRENDERED.
    """
    cwd, stack, out = ".", [], []
    for line in recipe.script_lines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # An expression render() can't evaluate (e.g. ${{ "x" if ppc64le else "" }})
        # must not be split into words that look like arguments.
        line = re.sub(r"\$\{\{.*?\}\}", " $UNRENDERED ", line)
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:  # unbalanced quotes
            continue
        command, redirect = [], False
        for token in tokens + [";"]:
            if redirect:  # the target of a redirection is not an argument
                redirect = False
                continue
            if not token:  # an empty quoted argument, e.g. from ${{ "" }}
                continue
            if token[0] in "<>":
                redirect = True
                continue
            if token not in ("&&", "||", ";", "|", "&"):
                command.append(token)
                continue
            while command and re.match(r"^\w+=", command[0]):
                command.pop(0)
            if not command:
                continue
            if command[0] in ("cd", "pushd"):
                if command[0] == "pushd":
                    stack.append(cwd)
                cwd = resolve(cwd, command[1] if len(command) > 1 else "$SRC_DIR")
            elif command[0] == "popd":
                cwd = stack.pop() if stack else None
            else:
                out.append((cwd, command))
            command = []
    return out


SRC_DIR = ("$SRC_DIR", "${SRC_DIR}", "%SRC_DIR%")


def resolve(cwd, path):
    """path, relative to cwd or under $SRC_DIR, as a path relative to the work directory.

    None if it can't be resolved: an unknown cwd, another variable, an absolute
    path, or a path outside the work directory.
    """
    if path is None:
        return None
    for root in SRC_DIR:
        if path == root or path.startswith(f"{root}/"):
            cwd, path = ".", path[len(root):].lstrip("/") or "."
            break
    if cwd is None or "$" in path or "%" in path or path.startswith("/"):
        return None
    resolved = posixpath.normpath(posixpath.join(cwd, path))
    return None if resolved.startswith("..") else resolved


def option(tokens, *names):
    """Values of the options names in tokens, as `--name value` or `--name=value`."""
    values = []
    for i, token in enumerate(tokens):
        for name in names:
            if token == name and i + 1 < len(tokens):
                values.append(tokens[i + 1])
            elif token.startswith(f"{name}="):
                values.append(token.split("=", 1)[1])
    return values


def tested_bins(recipe):
    """(binaries listed in package_contents tests, whether any such test is strict)."""
    bins, strict = [], False
    for test in recipe.tests():
        contents = test.get("package_contents") if isinstance(test, dict) else None
        if contents:
            bins += [render(b, recipe.context) for b in flatten(contents.get("bin"))]
            strict = strict or bool(contents.get("strict"))
    return bins, strict


def compare_bins(report, recipe, built, certain):
    """Compare the binaries the build produces with those the tests list.

    certain: whether `built` is known to be complete (Rust) or only a guess (Go).
    """
    tested, strict = tested_bins(recipe)
    report.info(f"binaries built: {sorted(built)} | tested in package_contents: {sorted(tested)} (strict: {strict})")
    missing = sorted(set(tested) - set(built))
    untested = sorted(set(built) - set(tested))
    if missing:
        (report.fail if certain else report.review)(f"package_contents tests binaries the build doesn't produce: {missing}")
    if untested and strict:
        (report.fail if certain else report.review)(f"strict package_contents test does not list built binaries: {untested}")
    elif untested:
        report.info(f"built binaries not tested: {untested}")


def compiler_versions(feedstock_root, keys, package):
    """Versions of a compiler that CI can use: pinned in .ci_support, else the newest on conda-forge."""
    pinned = set()
    for path in Path(feedstock_root, ".ci_support").glob("*.yaml"):
        config = yaml.safe_load(path.read_text()) or {}
        for key in keys:
            pinned.update(str(v) for v in config.get(key) or [])
    if pinned:
        return sorted(pinned, key=version_tuple), "pinned in .ci_support"
    try:
        latest = json.loads(fetch(f"https://api.anaconda.org/package/conda-forge/{package}"))["latest_version"]
    except Exception as e:  # the comparison is skipped, never guessed
        return [], f"newest conda-forge {package} unknown ({e})"
    return [latest], f"newest conda-forge {package}"


def version_tuple(version):
    return tuple(int(x) for x in re.findall(r"\d+", str(version)))


def check_toolchain(report, required, label, feedstock_root, keys, package):
    """Report if the source needs a newer compiler than conda-forge CI uses."""
    if not required:
        report.info(f"no minimum {label} declared")
        return
    available, where = compiler_versions(feedstock_root, keys, package)
    print(f"minimum {label}: {required} | {where}: {available}")
    if not available:
        report.review(f"check by hand that conda-forge's {package} satisfies {label} {required}")
    elif version_tuple(required) > version_tuple(available[0]):
        report.review(f"the source needs {label} {required}, newer than {where} {available[0]}: "
                      "CI will fail until conda-forge's compiler is updated")


def check_licenses(report, recipe, work, base_work, generated):
    """Check that license_file entries exist and include the bundled third-party licenses.

    generated: {path relative to the work dir (or None if unresolved): tool name}
    for the license bundles the build script writes.
    """
    report.section("licenses")
    license_files = recipe.license_files()
    print(f"about.license_file: {license_files}")
    entries = [e.rstrip("/") for e in license_files]
    for path, tool in generated.items():
        if path is None:
            report.review(f"could not resolve where {tool} writes its output; check that license_file packages it")
        elif not any(e == path or path.startswith(f"{e}/") or fnmatch.fnmatch(path, e) for e in entries):
            report.fail(f"{tool} writes {path}, but about.license_file does not package it")
    if not generated:
        report.review("the build script does not bundle third-party licenses "
                      "(cargo-bundle-licenses for Rust, go-licenses for Go)")
    for entry in entries:
        if any(path is not None and (entry == path or entry.startswith(f"{path}/")) for path in generated):
            continue
        if not (work.exists(entry) or work.glob(entry) or recipe.recipe_file(entry) is not None):
            report.fail(f"license_file {entry} is not in the source or the recipe directory")
    if base_work is None:
        return
    for entry in entries:
        # Only files from the main source have contents to compare.
        old, new = base_work.files.get(entry), work.files.get(entry)
        if old is not None and new is not None and old != new:
            report.review(f"license file {entry} changed since the base version")
    # License files at the top of the source that the recipe doesn't package.
    for f in work.listdir_files(work.prefix):
        if re.match(r"(LICEN[CS]E|COPYING|NOTICE)", posixpath.basename(f), re.IGNORECASE) and f not in entries:
            new = "new " if not base_work.is_file(f) else ""
            report.review(f"{new}top-level file {f} is not in about.license_file")
