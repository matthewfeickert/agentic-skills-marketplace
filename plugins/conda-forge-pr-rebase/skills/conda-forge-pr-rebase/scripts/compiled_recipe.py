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

import ast
import fnmatch
import hashlib
import io
import json
import posixpath
import re
import shlex
import subprocess
import tarfile
import zipfile
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

    def finish(self):
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
            text = self._recipe_file(name if name.endswith(".sh") else f"{name}.sh")
            if text is not None:
                lines = [text]
        # Join shell line continuations before splitting into lines.
        text = "\n".join(lines).replace("\\\n", " ")
        return [render(line, self.context) for line in text.splitlines()]

    def _recipe_file(self, name):
        rel = posixpath.join(posixpath.dirname(self.path), name)
        if self.base_ref:
            out = subprocess.run(["git", "show", f"{self.base_ref}:{rel}"], check=False, capture_output=True, text=True)
            return out.stdout if out.returncode == 0 else None
        p = Path(rel)
        return p.read_text() if p.exists() else None

    def recipe_dir_has(self, name):
        return self._recipe_file(name) is not None

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


class SourceTree:
    """A source archive read in memory, with its single top-level directory stripped.

    The archive is never extracted to disk, so its contents are only ever data.
    """

    def __init__(self, data):
        if zipfile.is_zipfile(io.BytesIO(data)):
            archive = zipfile.ZipFile(io.BytesIO(data))
            members = {i.filename: i for i in archive.infolist() if not i.is_dir()}
            self._read = lambda m: archive.read(m)
        else:
            # Kept open (no context manager): members are read lazily by _read.
            archive = tarfile.open(fileobj=io.BytesIO(data))
            members = {m.name: m for m in archive.getmembers() if m.isfile()}
            self._read = lambda m: archive.extractfile(m).read()
        tops = {name.split("/")[0] for name in members}
        # rattler-build strips a single top-level directory, as do GitHub and crates.io archives.
        strip = len(tops) == 1 and all("/" in n for n in members)
        self.files = {(n.split("/", 1)[1] if strip else n): m for n, m in members.items()}


class WorkDir:
    """The build's work directory: the main source mounted at its target_directory.

    Files from other sources are known by name only; they are not downloaded.
    """

    def __init__(self, recipe, data):
        main = recipe.main_source()
        prefix = render(main.get("target_directory", ""), recipe.context).strip("/")
        self.tree = SourceTree(data)
        self.prefix = prefix
        self.files = {posixpath.join(prefix, n) if prefix else n: n for n in self.tree.files}
        self.other = set()  # paths of files from the other sources
        self.uninspected = []
        for s in recipe.sources():
            if s is main or not isinstance(s, dict):
                continue
            target = render(s.get("target_directory", ""), recipe.context).strip("/")
            if "file_name" in s:
                self.other.add(posixpath.join(target, render(s["file_name"], recipe.context)) if target else render(s["file_name"], recipe.context))
            else:
                self.uninspected.append(s.get("url") or s.get("git") or s.get("path"))

    def read(self, path):
        member = self.tree.files[self.files[path]]
        return self.tree._read(member)

    def text(self, path):
        return self.read(path).decode(errors="replace")

    def is_file(self, path):
        return path in self.files or path in self.other

    def is_dir(self, path):
        path = path.rstrip("/")
        prefix = f"{path}/" if path not in ("", ".") else ""
        return any(f.startswith(prefix) for f in self.files) or any(f.startswith(prefix) for f in self.other)

    def exists(self, path):
        return self.is_file(path) or self.is_dir(path)

    def glob(self, pattern):
        return [f for f in list(self.files) + list(self.other) if fnmatch.fnmatch(f, pattern)]

    def listdir_files(self, path):
        prefix = "" if path in ("", ".") else f"{path.rstrip('/')}/"
        return [f for f in self.files if f.startswith(prefix) and "/" not in f[len(prefix):]]

    def find_up(self, start, name):
        """The nearest path start/name, start/../name, ... that exists, or None."""
        d = start
        while True:
            candidate = posixpath.join(d, name) if d not in ("", ".") else name
            if self.is_file(candidate):
                return candidate
            if d in ("", "."):
                return None
            d = posixpath.dirname(d)


def download_source(recipe, report=None):
    """Download the recipe's main source; report whether its sha256 matches."""
    main = recipe.main_source()
    urls = main["url"] if isinstance(main["url"], list) else [main["url"]]
    urls = [render(u, recipe.context) for u in urls]
    errors = []
    for url in urls:
        try:
            data = fetch(url)
            break
        except Exception as e:  # try the next URL in the list
            errors.append(f"{url}: {e}")
    else:
        raise RuntimeError("could not download source:\n  " + "\n  ".join(errors))
    if report is not None:
        print(f"source: {url}")
        sha = render(main.get("sha256", ""), recipe.context)
        if hashlib.sha256(data).hexdigest() == sha:
            print("recipe sha256 matches source: True")
        else:
            report.fail(f"recipe sha256 {sha} does not match the source ({hashlib.sha256(data).hexdigest()})")
    return WorkDir(recipe, data)


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
                cwd = _cd(cwd, command[1] if len(command) > 1 else "$SRC_DIR")
            elif command[0] == "popd":
                cwd = stack.pop() if stack else None
            else:
                out.append((cwd, command))
            command = []
    return out


def _cd(cwd, target):
    if target in ("$SRC_DIR", "${SRC_DIR}", "%SRC_DIR%"):
        return "."
    for root in ("$SRC_DIR/", "${SRC_DIR}/"):
        if target.startswith(root):
            return posixpath.normpath(target[len(root):])
    if cwd is None or "$" in target or "%" in target or target.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(cwd, target))
    return None if path.startswith("..") else path


def join(cwd, path):
    """path relative to the work directory, or None if it can't be resolved."""
    if cwd is None or path is None or "$" in path or "%" in path:
        return None
    for root in ("$SRC_DIR/", "${SRC_DIR}/"):
        if path.startswith(root):
            return posixpath.normpath(path[len(root):])
    if path.startswith("/"):
        return None
    return posixpath.normpath(posixpath.join(cwd, path))


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
    entries = [e.rstrip("/") for e in recipe.license_files()]
    print(f"about.license_file: {recipe.license_files()}")
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
        if not (work.exists(entry) or work.glob(entry) or recipe.recipe_dir_has(entry)):
            report.fail(f"license_file {entry} is not in the source or the recipe directory")
    if base_work is None:
        return
    for entry in entries:
        # Only files from the main source can be compared.
        if entry in work.files and entry in base_work.files and work.read(entry) != base_work.read(entry):
            report.review(f"license file {entry} changed since the base version")
    # License files at the top of the source that the recipe doesn't package.
    top = work.prefix or "."
    for f in work.listdir_files(top):
        if re.match(r"(LICEN[CS]E|COPYING|NOTICE)", posixpath.basename(f), re.IGNORECASE) and f not in entries:
            new = "new " if not base_work.is_file(f) else ""
            report.review(f"{new}top-level file {f} is not in about.license_file")
