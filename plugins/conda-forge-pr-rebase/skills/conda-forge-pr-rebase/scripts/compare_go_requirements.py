"""Compare a Go feedstock recipe with its source and with the base branch's version.

Usage (from the feedstock root):
    pixi exec --force-reinstall -s python -s pyyaml -- python compare_go_requirements.py [recipe/recipe.yaml] [--base-ref origin/main]

Go packages have no released metadata that lists conda requirements, so this
compares the PR's source with the source of the version in --base-ref's recipe.
It downloads both, checks the PR's sha256, and follows the linux-64 build
script to the packages that `go build`/`go install`/`go run` use. Then it reports:
  - package paths the build uses that moved since the base version (MISMATCH)
  - a go directive in go.mod newer than conda-forge's go compiler (REVIEW)
  - module requirements added, removed or changed in go.mod (info), and a
    changed module path (REVIEW)
  - cgo use (`import "C"`) with the go-nocgo compiler (REVIEW)
  - binaries that package_contents tests but the build doesn't produce (REVIEW)
  - third-party licenses from go-licenses that about.license_file doesn't
    package, and license_file entries missing from the source (MISMATCH)
  - license changes since the base version (REVIEW)
Only v1 recipe.yaml with a single output is supported.
Exits 0 on a match, 1 on any mismatch, and 2 if the script fails to run.
"""

import argparse
import posixpath
import re
import sys
import traceback
from pathlib import Path

from compiled_recipe import (
    Report,
    check_licenses,
    check_toolchain,
    commands,
    compare_bins,
    download_source,
    join,
    load_recipe,
    option,
    version_tuple,
)

GO_COMPILERS = {"compiler:go", "compiler:go-nocgo", "compiler:go-cgo"}
# `go build` flags that take a value as the next token.
VALUE_FLAGS = {
    "-o", "-C", "-p", "-ldflags", "-gcflags", "-asmflags", "-gccgoflags", "-tags",
    "-buildmode", "-mod", "-modfile", "-pkgdir", "-toolexec", "-overlay",
    "-pgo", "-compiler", "-installsuffix", "-covermode", "-coverpkg",
}
# Modules that need cgo, and so the go-cgo compiler and a C compiler.
CGO_MODULES = {
    "github.com/mattn/go-sqlite3", "github.com/libgit2/git2go", "github.com/confluentinc/confluent-kafka-go",
    "github.com/go-gl/glfw", "github.com/miekg/pkcs11",
}
IMPORT_C = re.compile(r'^import\s+"C"', re.MULTILINE)


def packages(tokens):
    """(package arguments, -o value) of a `go build/install/run` command."""
    args, i = [], 2
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-"):
            i += 2 if token.split("=")[0] in VALUE_FLAGS and "=" not in token else 1
            continue
        if "$" in token:  # an unrendered expression or a variable, not a package path
            i += 1
            continue
        args.append(token)
        if tokens[1] == "run":
            break  # later arguments are the program's own
        i += 1
    outputs = option(tokens, "-o")
    return args or ["."], outputs[0] if outputs else None


def analyse(recipe):
    """The local packages the build uses, the binaries it writes, and the license bundles."""
    builds, generated = [], {}
    for cwd, tokens in commands(recipe):
        if tokens[0] == "go" and len(tokens) > 1 and tokens[1] in ("build", "install", "run"):
            pkgs, output = packages(tokens)
            for pkg in pkgs:
                if pkg.startswith("."):
                    builds.append({"cmd": tokens[1], "raw": pkg, "dir": join(cwd, pkg.removesuffix("/...")),
                                   "all": pkg.endswith("..."), "output": output if len(pkgs) == 1 else None})
        elif tokens[0] == "go-licenses" and tokens[1:2] == ["save"]:
            outputs = option(tokens, "--save_path")
            generated[join(cwd, outputs[0]) if outputs else None] = "go-licenses"
    return builds, generated


def is_main(work, directory):
    for f in work.listdir_files(directory):
        if f.endswith(".go") and not f.endswith("_test.go") and re.search(r"^package main\b", work.text(f), re.MULTILINE):
            return True
    return False


def parse_gomod(text):
    mod = {"module": None, "go": None, "toolchain": None, "require": {}}
    block = None
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        indirect = "// indirect" in raw
        if block:
            if line == ")":
                block = None
            elif block == "require" and line:
                name, version = line.split()[:2]
                mod["require"][name] = (version, indirect)
            continue
        if m := re.match(r"(module|go|toolchain)\s+(\S+)", line):
            mod[m[1]] = m[2].strip('"')
        elif m := re.match(r"(\w+)\s*\($", line):
            block = m[1]
        elif m := re.match(r"require\s+(\S+)\s+(\S+)", line):
            mod["require"][m[1]] = (m[2], indirect)
    return mod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", nargs="?", default="recipe/recipe.yaml")
    parser.add_argument("--base-ref", default="origin/main", help="git ref with the base version's recipe")
    args = parser.parse_args()
    feedstock_root = Path(args.recipe).resolve().parent.parent
    report = Report()

    recipe = load_recipe(args.recipe)
    compilers = GO_COMPILERS & set(recipe.requirements("build"))
    if not compilers:
        raise RuntimeError("not a Go recipe: no ${{ compiler('go-nocgo') }} or go-cgo in requirements.build")
    cgo = "compiler:go-cgo" in compilers
    print(f"recipe version: {recipe.version} | compiler: {', '.join(sorted(compilers))}")
    work = download_source(recipe, report)

    base = load_recipe(args.recipe, args.base_ref)
    base_work = None
    if base is None:
        report.info(f"no recipe at {args.base_ref}; nothing to compare against")
    elif base.version == recipe.version:
        report.info(f"{args.base_ref} has the same version {base.version} (rebuild); source unchanged")
    else:
        print(f"base version ({args.base_ref}): {base.version}")
        base_work = download_source(base)

    report.section("go build")
    builds, generated = analyse(recipe)
    if not builds:
        report.review("no `go build`/`go install` of a local package in the linux-64 build script "
                      "(e.g. it builds with make); package paths and binaries not checked")
    modules, built = set(), set()
    for b in builds:
        print(f"go {b['cmd']} {b['raw']} -> {b['dir']} (-o {b['output']})")
        if b["dir"] is None:
            report.review(f"could not resolve `go {b['cmd']} {b['raw']}`; check the package by hand")
            continue
        if not work.is_dir(b["dir"]):
            moved = base_work is not None and base_work.is_dir(b["dir"])
            msg = f"package directory {b['dir']} is not in the source"
            (report.fail if moved else report.review)(msg + (" (it existed in the base version)" if moved else ""))
            continue
        if not b["all"] and b["cmd"] != "run" and not is_main(work, b["dir"]):
            report.review(f"{b['dir']} is not a main package")
        gomod = work.find_up(b["dir"], "go.mod")
        if gomod is None:
            report.review(f"no go.mod above {b['dir']}")
        else:
            modules.add(gomod)
        if b["cmd"] == "run" or b["all"]:
            continue
        if b["output"]:
            out = b["output"]
            # `-o $PREFIX/bin/` names the binary after the package.
            built.add(posixpath.basename(out) if not out.endswith("/") else posixpath.basename(b["dir"]))
    if builds:
        # Not certain: without -o, Go names a binary after its module or directory.
        compare_bins(report, recipe, {name.removesuffix(".exe") for name in built}, certain=False)
    if not modules:
        top = f"{work.prefix}/go.mod" if work.prefix else "go.mod"
        if work.is_file(top):
            report.info(f"comparing the top-level {top}")
            modules.add(top)

    go_directives = []
    for gomod in sorted(modules):
        report.section(gomod)
        mod = parse_gomod(work.text(gomod))
        print(f"module {mod['module']} | go {mod['go']} | toolchain {mod['toolchain']} | {len(mod['require'])} requirements")
        if mod["go"]:
            go_directives.append(mod["go"])
        old = parse_gomod(base_work.text(gomod)) if base_work and base_work.is_file(gomod) else None
        if base_work and old is None:
            report.review(f"no {gomod} in the base version")
        if old:
            if old["module"] != mod["module"]:
                report.review(f"module path changed: {old['module']} -> {mod['module']}")
            if old["go"] != mod["go"]:
                report.info(f"go directive changed: {old['go']} -> {mod['go']}")
            new_req, old_req = mod["require"], old["require"]
            print(f"added requirements: {sorted(set(new_req) - set(old_req))}")
            print(f"removed requirements: {sorted(set(old_req) - set(new_req))}")
            changed = sorted(f"{n} {old_req[n][0]} -> {new_req[n][0]}"
                             for n in set(new_req) & set(old_req) if new_req[n][0] != old_req[n][0])
            print(f"changed requirements ({len(changed)}): {changed}")
        added = set(mod["require"]) - set(old["require"] if old else {})
        for name in sorted(added & CGO_MODULES):
            if not cgo:
                report.review(f"new requirement {name} needs cgo: the recipe uses go-nocgo")

        # cgo in the module itself, or in vendored dependencies.
        root = posixpath.dirname(gomod)
        prefix = f"{root}/" if root else ""
        cgo_files = [f for f in work.files if f.startswith(prefix) and f.endswith(".go")
                     and not f.endswith("_test.go") and IMPORT_C.search(work.text(f))]
        vendored = work.is_dir(posixpath.join(root, "vendor")) if root else work.is_dir("vendor")
        print(f"files with import \"C\": {len(cgo_files)} (vendored dependencies scanned: {vendored})")
        if cgo_files and not cgo:
            report.review(f"{len(cgo_files)} files use cgo (e.g. {cgo_files[:3]}) but the recipe uses go-nocgo; "
                          "fine if they are behind a cgo build tag with a pure-Go fallback")
        elif cgo and not cgo_files:
            report.info("the recipe uses go-cgo, but no scanned file imports \"C\" (a dependency may)")

    report.section("go directive")
    check_toolchain(report, max(go_directives, key=version_tuple) if go_directives else None,
                    "go version", feedstock_root, ["go_compiler_version", "cgo_compiler_version"], "go")

    check_licenses(report, recipe, work, base_work, generated)
    for source in work.uninspected:
        report.review(f"additional source not inspected: {source}")
    sys.exit(report.finish())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Exit 2, not 1, so a script that could not run (download failure,
        # unsupported recipe layout, ...) is never read as a requirements mismatch.
        traceback.print_exc()
        print("RESULT: ERROR (the script failed; this is not a requirements mismatch)")
        sys.exit(2)
