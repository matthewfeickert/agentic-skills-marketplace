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

import posixpath
import re
from pathlib import Path

from compiled_recipe import (
    Report,
    check_licenses,
    check_toolchain,
    commands,
    compare_bins,
    load_recipe,
    load_sources,
    option,
    parse_args,
    print_changes,
    resolve,
    run,
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
IMPORT_C = re.compile(rb'^import\s+"C"', re.MULTILINE)


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
                    builds.append({"cmd": tokens[1], "raw": pkg, "dir": resolve(cwd, pkg.removesuffix("/...")),
                                   "all": pkg.endswith("..."), "output": output if len(pkgs) == 1 else None})
        elif tokens[0] == "go-licenses" and tokens[1:2] == ["save"]:
            outputs = option(tokens, "--save_path")
            generated[resolve(cwd, outputs[0]) if outputs else None] = "go-licenses"
    return builds, generated


def is_main(work, directory):
    for f in work.listdir_files(directory):
        if f.endswith(".go") and not f.endswith("_test.go") and re.search(r"^package main\b", work.text(f), re.MULTILINE):
            return True
    return False


def parse_gomod(text):
    """The module path, go and toolchain directives, and {module: version} requirements."""
    mod = {"module": None, "go": None, "toolchain": None, "require": {}}
    block = None
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        if block:
            if line == ")":
                block = None
            elif block == "require" and line:
                name, version = line.split()[:2]
                mod["require"][name] = version
            continue
        if m := re.match(r"(module|go|toolchain)\s+(\S+)", line):
            mod[m[1]] = m[2].strip('"')
        elif m := re.match(r"(\w+)\s*\($", line):
            block = m[1]
        elif m := re.match(r"require\s+(\S+)\s+(\S+)", line):
            mod["require"][m[1]] = m[2]
    return mod


def main():
    args = parse_args()
    feedstock_root = Path(args.recipe).resolve().parent.parent
    report = Report()

    recipe = load_recipe(args.recipe)
    compilers = GO_COMPILERS & set(recipe.requirements("build"))
    if not compilers:
        raise RuntimeError("not a Go recipe: no ${{ compiler('go-nocgo') }} or go-cgo in requirements.build")
    cgo = "compiler:go-cgo" in compilers
    print(f"recipe version: {recipe.version} | compiler: {', '.join(sorted(compilers))}")
    work, _, base_work = load_sources(recipe, args.base_ref, report)

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
        top = resolve(work.prefix, "go.mod")
        if work.is_file(top):
            report.info(f"comparing the top-level {top}")
            modules.add(top)

    go_directives = []
    # Scanned once for all modules: cgo in the modules themselves, or in vendored dependencies.
    cgo_files = sorted(f for f, content in work.files.items() if content is not None and f.endswith(".go")
                       and not f.endswith("_test.go") and IMPORT_C.search(content)) if modules else []
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
            print_changes("requirements", mod["require"], old["require"])
        if not cgo:
            added = set(mod["require"]) - set(old["require"] if old else {})
            for name in sorted(added & CGO_MODULES):
                report.review(f"new requirement {name} needs cgo: the recipe uses go-nocgo")

        root = posixpath.dirname(gomod) or "."
        module_cgo = [f for f in cgo_files if root == "." or f.startswith(f"{root}/")]
        vendored = work.is_dir(resolve(root, "vendor"))
        print(f"files with import \"C\": {len(module_cgo)} (vendored dependencies scanned: {vendored})")
        if module_cgo and not cgo:
            report.review(f"{len(module_cgo)} files use cgo (e.g. {module_cgo[:3]}) but the recipe uses go-nocgo; "
                          "fine if they are behind a cgo build tag with a pure-Go fallback")
        elif cgo and not module_cgo:
            report.info("the recipe uses go-cgo, but no scanned file imports \"C\" (a dependency may)")

    report.section("go directive")
    check_toolchain(report, max(go_directives, key=version_tuple) if go_directives else None,
                    "go version", feedstock_root, ["go_compiler_version", "cgo_compiler_version"], "go")

    check_licenses(report, recipe, work, base_work, generated)
    return report.finish(work)


if __name__ == "__main__":
    run(main)
