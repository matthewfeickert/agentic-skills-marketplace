"""Compare a Rust feedstock recipe with its source and with the base branch's version.

Usage (from the feedstock root):
    pixi exec --force-reinstall -s python -s pyyaml -- python compare_rust_requirements.py [recipe/recipe.yaml] [--base-ref origin/main]

Rust packages have no released metadata that lists conda requirements, so this
compares the PR's source with the source of the version in --base-ref's recipe.
It downloads both, checks the PR's sha256, and follows the linux-64 build
script to the crates that `cargo install --path` builds. Then it reports:
  - a missing Cargo.lock when the build uses --locked (MISMATCH)
  - binaries that package_contents tests but the crates no longer define, or
    new binaries that a strict package_contents test doesn't list (MISMATCH)
  - a rust-version newer than conda-forge's rust compiler (REVIEW)
  - crates added, removed or changed in Cargo.lock, and new crates that build
    or link native code (-sys crates, cmake, bindgen, ...) (REVIEW)
  - changes to build.rs (REVIEW)
  - third-party licenses from cargo-bundle-licenses that about.license_file
    doesn't package, and license_file entries missing from the source (MISMATCH)
  - license changes since the base version (REVIEW)
Only v1 recipe.yaml with a single output is supported.
Exits 0 on a match, 1 on any mismatch, and 2 if the script fails to run.
"""

import argparse
import posixpath
import sys
import tomllib
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

# -sys crates for native libraries conda-forge packages, and the variable that
# makes the crate use the system library where it would otherwise build its own.
NATIVE_SYS = {
    "openssl-sys": ("openssl", "OPENSSL_NO_VENDOR=1"),
    "libgit2-sys": ("libgit2", "LIBGIT2_NO_VENDOR=1"),
    "libssh2-sys": ("libssh2", "LIBSSH2_SYS_USE_PKG_CONFIG=1"),
    "libz-sys": ("zlib", None),
    "zstd-sys": ("zstd", "ZSTD_SYS_USE_PKG_CONFIG=1"),
    "libsqlite3-sys": ("libsqlite", None),
    "bzip2-sys": ("bzip2", None),
    "curl-sys": ("libcurl", None),
}
# -sys crates that only bind OS or runtime APIs and build no native library.
PURE_BINDINGS = {
    "windows-sys", "linux-raw-sys", "core-foundation-sys", "js-sys", "web-sys",
    "security-framework-sys", "system-configuration-sys", "io-kit-sys",
    "fsevent-sys", "inotify-sys", "objc-sys", "objc2-sys", "wasi", "dirs-sys",
}
# Build-script helper crates and the build requirement each one needs.
BUILD_TOOLS = {"cmake": "cmake", "pkg-config": "pkg-config", "bindgen": "libclang", "cc": "compiler:c"}


def manifest(work, directory):
    path = posixpath.join(directory, "Cargo.toml") if directory != "." else "Cargo.toml"
    return tomllib.loads(work.text(path)) if work.is_file(path) else None


def workspace_root(work, directory):
    """The directory and manifest of the workspace that contains directory."""
    d = directory
    while True:
        m = manifest(work, d)
        if m is not None and "workspace" in m:
            return d, m
        if d in ("", "."):
            return directory, manifest(work, directory)
        d = posixpath.dirname(d) or "."


def inherited(package, key, ws_manifest):
    value = package.get(key)
    if isinstance(value, dict) and value.get("workspace"):
        return ((ws_manifest or {}).get("workspace", {}).get("package", {})).get(key)
    return value


def binaries(work, directory, man):
    """Names of the binary targets of the package in directory (Cargo's target auto-discovery)."""
    def rel(p):
        return posixpath.normpath(posixpath.join(directory, p))

    package = man["package"]
    names, paths = set(), set()
    for target in man.get("bin", []):
        names.add(target["name"])
        default = "src/main.rs" if target["name"] == package["name"] else f"src/bin/{target['name']}.rs"
        paths.add(rel(target.get("path", default)))
    if package.get("autobins", True):
        auto = {}
        if work.is_file(rel("src/main.rs")):
            auto[rel("src/main.rs")] = package["name"]
        for f in work.files:
            if f.startswith(rel("src/bin") + "/"):
                parts = f[len(rel("src/bin")) + 1:].split("/")
                if len(parts) == 1 and parts[0].endswith(".rs"):
                    auto[f] = parts[0][:-3]
                elif len(parts) == 2 and parts[1] == "main.rs":
                    auto[f] = parts[0]
        for path, name in auto.items():
            if path not in paths and name not in names:
                names.add(name)
    return names


def analyse(recipe, work):
    """The crates the build installs and what the build script bundles."""
    installs, generated = [], {}
    for cwd, tokens in commands(recipe):
        rest = tokens[1:]
        if tokens[0] == "cargo" and rest[:1] == ["auditable"]:
            rest = rest[1:]
        if tokens[0] == "cargo" and rest[:1] == ["install"]:
            paths = option(rest, "--path")
            installs.append({
                "dir": join(cwd, paths[0]) if paths else None,
                "raw": paths[0] if paths else None,
                "locked": "--locked" in rest,
                "bins": option(rest, "--bin"),
            })
        elif tokens[0] == "cargo-bundle-licenses":
            outputs = option(tokens, "--output", "-o")
            generated[join(cwd, outputs[0]) if outputs else None] = "cargo-bundle-licenses"
    return installs, generated


def lock_packages(work, path):
    """{crate name: {versions}} from Cargo.lock at path."""
    packages = {}
    for p in tomllib.loads(work.text(path)).get("package", []):
        packages.setdefault(p["name"], set()).add(p["version"])
    return packages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", nargs="?", default="recipe/recipe.yaml")
    parser.add_argument("--base-ref", default="origin/main", help="git ref with the base version's recipe")
    args = parser.parse_args()
    feedstock_root = Path(args.recipe).resolve().parent.parent
    report = Report()

    recipe = load_recipe(args.recipe)
    if "compiler:rust" not in recipe.requirements("build"):
        raise RuntimeError("not a Rust recipe: no ${{ compiler('rust') }} in requirements.build")
    print(f"recipe version: {recipe.version}")
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
    base_installs, _ = analyse(base, base_work) if base_work else ([], {})

    report.section("cargo install")
    installs, generated = analyse(recipe, work)
    if not installs:
        report.review("no `cargo install` in the linux-64 build script; binaries and Cargo.lock not checked")
    built, certain, msrvs, lock_path = set(), bool(installs), [], None
    for inst in installs:
        print(f"cargo install --path {inst['raw']} -> {inst['dir']} (locked: {inst['locked']})")
        if inst["dir"] is None:
            report.review(f"could not resolve `--path {inst['raw']}`; check the crate by hand")
            certain = False
            continue
        man = manifest(work, inst["dir"])
        if man is None or "package" not in man:
            moved = any(b["dir"] == inst["dir"] and manifest(base_work, b["dir"]) for b in base_installs)
            msg = f"no package Cargo.toml at {inst['dir']}"
            (report.fail if moved else report.review)(msg + (" (it existed in the base version)" if moved else ""))
            certain = False
            continue
        ws_dir, ws_man = workspace_root(work, inst["dir"])
        lock = posixpath.join(ws_dir, "Cargo.lock") if ws_dir != "." else "Cargo.lock"
        if not work.is_file(lock):
            (report.fail if inst["locked"] else report.review)(f"no Cargo.lock at {lock} (the build uses --locked: {inst['locked']})")
        else:
            lock_path = lock_path or lock
        names = binaries(work, inst["dir"], man)
        built |= set(inst["bins"]) if inst["bins"] else names
        msrv = inherited(man["package"], "rust-version", ws_man)
        if msrv:
            msrvs.append(str(msrv))
        print(f"crate {man['package']['name']} (workspace root: {ws_dir}): bins {sorted(names)}, rust-version {msrv}")

        # build.rs compiles and may link native code, so any change matters.
        build_rs = man["package"].get("build", "build.rs")
        build_rs = posixpath.normpath(posixpath.join(inst["dir"], build_rs)) if isinstance(build_rs, str) else None
        if base_work and build_rs and work.is_file(build_rs):
            if build_rs not in base_work.files:
                report.review(f"new build script {build_rs}")
            elif work.read(build_rs) != base_work.read(build_rs):
                report.review(f"{build_rs} changed since the base version; check it for new native dependencies")

        if base_work:
            base_man = manifest(base_work, inst["dir"])
            if base_man and "package" in base_man:
                old = inherited(base_man["package"], "license", workspace_root(base_work, inst["dir"])[1])
                new = inherited(man["package"], "license", ws_man)
                if old != new:
                    report.review(f"crate license changed: {old} -> {new} (update about.license)")
    if installs:
        compare_bins(report, recipe, built, certain)

    report.section("rust-version")
    check_toolchain(report, max(msrvs, key=version_tuple) if msrvs else None,
                    "rust-version", feedstock_root, ["rust_compiler_version"], "rust")

    if lock_path:
        report.section(f"{lock_path} changes")
        packages = lock_packages(work, lock_path)
        base_packages = {}
        if base_work and base_work.is_file(lock_path):
            base_packages = lock_packages(base_work, lock_path)
            added = sorted(set(packages) - set(base_packages))
            removed = sorted(set(base_packages) - set(packages))
            changed = sorted(f"{n} {','.join(sorted(base_packages[n]))} -> {','.join(sorted(packages[n]))}"
                             for n in set(packages) & set(base_packages) if packages[n] != base_packages[n])
            print(f"added crates: {added}")
            print(f"removed crates: {removed}")
            print(f"changed crates ({len(changed)}): {changed}")
        elif base_work:
            report.review(f"no {lock_path} in the base version; every crate is compared as new")
        new = set(packages) - set(base_packages)

        report.section("native code")
        host = set(recipe.requirements("host"))
        build = set(recipe.requirements("build"))
        env = recipe.script_env()
        script = "\n".join(recipe.script_lines())
        sys_crates = sorted(n for n in packages if n.endswith(("-sys", "_sys")) and n not in PURE_BINDINGS)
        print(f"-sys crates in Cargo.lock: {sys_crates}")
        for name in sys_crates:
            if name not in new:
                continue  # the base version already built with it
            if name in NATIVE_SYS:
                lib, var = NATIVE_SYS[name]
                hint = ""
                if var and var.split("=")[0] not in env and var.split("=")[0] not in script:
                    hint = f", and set {var} in build.script.env so it doesn't build its own copy"
                if lib not in host:
                    report.review(f"new crate {name} links {lib}: add {lib} to host and pkg-config to build{hint}, "
                                  "or confirm that building its bundled copy is intended")
                elif hint:
                    report.review(f"new crate {name}: {lib} is in host{hint}")
            else:
                report.review(f"new crate {name} may build or link a native library; check its build.rs and features")
        for crate, need in BUILD_TOOLS.items():
            if crate in new and need not in build:
                report.review(f"new crate {crate} is used by a build script; it may need {need.replace('compiler:', 'compiler ')} in build")
        report.info("Cargo.lock lists crates for every feature and target, so a new crate may not be built at all")

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
