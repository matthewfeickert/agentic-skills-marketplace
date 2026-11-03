---
name: conda-forge-pr-rebase
description: Rebase an open conda-forge feedstock PR (typically a regro-cf-autotick-bot version bump) onto the head of the upstream main branch, validate the recipe's run and host requirements against the package metadata (a Python sdist's PKG-INFO and build-system table, an R package's DESCRIPTION, or for Rust and Go packages the Cargo.lock or go.mod, binaries, toolchain version and bundled third-party licenses compared with the previous version's source), add a requirements commit if needed, regenerate with conda-smithy, and force-push to the PR branch. Use when the user asks to rebase a conda-forge feedstock PR or bring a bot version-bump PR up to date with main.
argument-hint: <conda-forge feedstock PR URL>
allowed-tools: Bash, Read, Edit, Write
---

Rebase the conda-forge feedstock PR given in `$ARGUMENTS` onto upstream `main`, make its run and host requirements match the released package's metadata, regenerate, and push back to the PR's branch.

## Scope rules

- Only touch the PR you were asked about. Never close, merge, comment on, or recommend actions on other PRs (e.g. older superseded bot PRs), even if they look stale.
- Push only to the PR's own head branch. Never push to `conda-forge/<feedstock>` itself.
- Only modify files under `recipe/`; everything else comes from regeneration.
- Work in a clone under the session scratchpad directory, and report its absolute path at the end.
- The clone's directory must be named exactly `<name>-feedstock` (the `gh repo clone` default). Never rename it (e.g. to `pr<N>`) and never use a `git worktree` with a different directory name. `conda-smithy` takes the feedstock name from the directory name and otherwise writes the wrong name into `pixi.toml` and `README.md`. To keep clones for several PRs apart, put each in its own parent directory (e.g. `<scratchpad>/pr<N>/<name>-feedstock`).
- Always run `pixi exec` with `--force-reinstall`, as in the commands below. Without it, `pixi exec` reuses any cached environment with the same specs and never checks for newer packages, so an unpinned tool such as `rattler-build` stays at whatever version was first resolved. A local build would then no longer match the `rattler-build` that conda-forge CI uses. The flag re-resolves and relinks from the package cache, which takes about a second.

## 1. Inspect the PR and set up the clone

```bash
gh pr view <N> -R conda-forge/<feedstock> --json headRefName,headRefOid,headRepositoryOwner,maintainerCanModify,state,title
```

Record `headRefOid` (the old head, needed for the lease when pushing), the head owner, and the branch. The [preflight checks](#preflight-checks) below use `state`, the head owner, and `maintainerCanModify`.

```bash
gh repo clone conda-forge/<feedstock> -- -q    # or reuse an existing clone and `git fetch origin main`
git remote add bot https://github.com/<head-owner>/<feedstock>.git
git fetch bot <head-branch>
git switch -C pr<N> bot/<head-branch>
git log --format='%h %an: %s' origin/main..pr<N>
```

Always re-fetch `main` right before rebasing; earlier PRs in a series may have just been merged. If the upstream remote is named `upstream` (a clone of a personal fork), use `upstream/main` in place of `origin/main` throughout.

Also look at the repository's commit history (`git log --format='%h %an: %s' origin/main | grep -v -e regro -e 'conda-forge administrator' -e 'conda-forge-webservices'`) to learn the human commit message style. On the user's feedstocks it is `MNT: <Imperative description>`, and requirement updates are `MNT: Update to v<version> requirements`.

### Preflight checks

Run these after fetching `main` and before changing anything. If any of them fails, stop: don't rebase or push. Tell the user which check failed and what you found, and leave any action on the PR itself (e.g. closing a superseded PR) to them.

1. **Tools:** `command -v git gh pixi curl conda-smithy` prints all five paths, and `gh auth status` shows a logged-in account.
2. **The PR is open:** `state` is `OPEN`.
3. **The branch can be pushed:** the head owner is not `conda-forge` (the scope rules forbid pushing to `conda-forge/<feedstock>`). Unless the head repository belongs to the user (`gh api user -q .login`), `maintainerCanModify` must be `true`, or GitHub refuses the push.
4. **The version is not a downgrade.** Compare the version on `main` with the PR's:

   ```bash
   git show origin/main:recipe/recipe.yaml | grep -m1 -E '^\s+version:'      # main
   git show bot/<head-branch>:recipe/recipe.yaml | grep -m1 -E '^\s+version:'  # PR
   ```

   For a v0 `meta.yaml`, grep for `set version` instead. For a version-bump PR, the PR's version must be newer than `main`'s. If `main` already has the same or a newer version, the PR is superseded, and resolving the rebase conflict would downgrade the recipe. For a rebuild or migration PR (same version, higher build number; see [Rebuild PRs](#rebuild-prs-keep-the-build-number-above-mains)), the versions must be equal.

Also check whether the recipe is multi-output (a top-level `outputs:` key). That is not a reason to stop, but the step 3 scripts can't compare such a recipe and exit with code 2. Plan to compare each output's requirements by hand in step 3, and say so in the report.

## 2. Rebase

```bash
git rebase origin/main
```

Git will usually drop the bot's `MNT: Re-rendered with conda-smithy ...` commit automatically ("skipped previously applied commit" or "patch contents already upstream") because `main` already carries identical rendered files. That is expected. If the branch is already based on `main` ("Current branch is up to date"), nothing is dropped and the bot's regeneration commit stays in the history. Note that it's there: step 5 needs to know about it.

### Resolving the version-bump conflict

The typical conflict is in `recipe/recipe.yaml` on `context.version` and `source.sha256`, because `main` moved to a newer version since the bot branched.

**Do not resolve with `git checkout --theirs recipe/recipe.yaml`.** That takes the bot's entire file and silently reverts every other change `main` made since the bot branched (removed dependencies, raised floors, build number changes). Resolve so that the bot's commit changes only `version` and `sha256` relative to `main`:

```bash
git show origin/main:recipe/recipe.yaml \
  | sed 's/version: "<main-version>"/version: "<pr-version>"/; s/sha256: <main-sha>/sha256: <pr-sha>/' \
  > recipe/recipe.yaml
git add recipe/recipe.yaml
GIT_EDITOR=true git rebase --continue
```

If `main` has a non-zero `build.number` (from a maintenance PR) and the PR changes the version, reset `build.number` to `0` in this same resolution.

For a v0 `meta.yaml` (common for R feedstocks), the same approach applies to `recipe/meta.yaml`, where the version lives in `{% set version = "<version>" %}`. In CRAN-generated recipes the sha256 is usually `sha256: <hash>` under `source`, so adjust the `sed` patterns to match.

If a stale rerender commit from the bot conflicts in generated files (e.g. `pixi.toml` with an older conda-smithy version than `main`), keep `main`'s side (`git checkout --ours <file>`). If the commit then has no changes left, `git rebase --skip` it.

After the rebase, confirm the version commit is minimal. Do this even when the rebase was a no-op, because the bot's own commit may change more than it should:

```bash
git diff origin/main -- recipe/    # should show only version + sha256 (+ build number reset if applicable)
```

### Rebuild PRs: keep the build number above `main`'s

Migration PRs (e.g. "Rebuild for gsl 2.8", with a new `.ci_support/migrations/*.yaml`) and other rebuilds keep the version and bump `build.number` instead. If `main` merged another rebuild since the bot branched, both made the identical bump (e.g. `0` → `1`). Git then silently drops the PR's bump during the rebase, with no conflict. The PR would rebuild at the same build number as the package already on `main`.

So when the PR's `context.version` equals `main`'s, check the build numbers after the rebase:

```bash
git show origin/main:recipe/recipe.yaml | grep -m1 -E '^\s+number:'   # main
grep -m1 -E '^\s+number:' recipe/recipe.yaml                          # PR
```

The PR's build number must be greater than `main`'s. If it isn't, set it to `main`'s build number + 1 in a **separate commit** after the bot's commits, e.g. `MNT: Bump build number for <migration> rebuild`. In the body, explain that `main` was rebuilt at the same build number, so the rebase dropped the migration's identical bump. This commit comes after the bot's regeneration commit, so step 5's rebuild rule then applies to keep the regeneration commit last.

### Check `source.url`

For a source from PyPI, `source.url` must use the version-agnostic form from conda-forge's [pure-Python example recipe](https://conda-forge.org/docs/maintainer/example_recipes/pure-python/): `https://files.pythonhosted.org/packages/source/<l>/<name>/<sdist-filename>`, with `${{ version }}` in the filename (which may use `_` where the package name uses `-`). Any `source.url` change in the diff above needs a look, as do two other forms:

- **Hash-based path:** the bot's version commit sometimes rewrites `source.url` to `https://files.pythonhosted.org/packages/ab/cd/<60-hex-hash>/<name>-${{ version }}.tar.gz`, e.g. when the redirect was returning 503s at the time. That hash is specific to one file, so the URL breaks on the next version update.
- **Older PyPI hosts:** `https://pypi.org/packages/source/...` or `https://pypi.io/packages/source/...`, often already on `main`.

If the URL has either form, after the PR's changes or already on `main`:

1. Confirm the `files.pythonhosted.org/packages/source` URL serves the same sdist, with the `curl` command below. If the redirect returns 503, retry after a short wait; it fails intermittently.
2. Change the URL in a **separate commit** directly after the bot's version commit, with a body that explains why, e.g. `MNT: Use files.pythonhosted.org source URL`. Don't edit the bot's commit, so the change stays visible.

The validation script in step 3 also flags any other PyPI URL form as a mismatch. It doesn't check sources from elsewhere, such as GitHub archives.

Verify the sha256 independently:

```bash
curl -sL https://files.pythonhosted.org/packages/source/<l>/<name>/<sdist-filename> | sha256sum
```

If the PR's CI failed with a `503 Service Unavailable` while downloading the source, that is a transient PyPI outage, not a recipe problem. Don't change the URL to work around it; report it so the job can be rerun.

## 3. Validate run and host requirements against the package metadata

Pick the script by package type, from the compilers in `requirements.build`:

- `${{ compiler('rust') }}`: `compare_rust_requirements.py`; see [Rust and Go packages](#rust-and-go-packages).
- `${{ compiler('go-nocgo') }}` or `${{ compiler('go-cgo') }}`: `compare_go_requirements.py`; see [Rust and Go packages](#rust-and-go-packages).
- R packages (`r-*` feedstocks, built with `R CMD INSTALL`): `compare_r_requirements.py`; see [R packages](#r-packages).
- Python packages from PyPI: `compare_requirements.py`; see [Python packages](#python-packages).

For any other package type no script applies. Compare the requirements by hand against the upstream build files, and tell the user that no script covers it.

All scripts end with a `RESULT:` line and an exit code:

- `RESULT: MATCH`, exit 0: the recipe matches the metadata.
- `RESULT: MISMATCH`, exit 1: the recipe differs from the metadata; the lines above it say how.
- `RESULT: ERROR`, exit 2: the script itself failed, e.g. a download kept failing after retries or the recipe has a layout the script doesn't support (such as a multi-output recipe). This says nothing about the recipe, so never change requirements because of it. Read the traceback, and fix the invocation if that was the cause (e.g. pass `--pypi-name`). Otherwise compare the requirements by hand and tell the user that the script could not run and why.

### Python packages

Run the bundled script from the feedstock root:

```bash
pixi exec --force-reinstall -s python -s pyyaml -s packaging -- python "${CLAUDE_SKILL_DIR}"/scripts/compare_requirements.py recipe/recipe.yaml
```

It downloads the sdist for the recipe's version and checks that `source.sha256` matches it and that a PyPI `source.url` has the `files.pythonhosted.org/packages/source` form (see [Check `source.url`](#check-sourceurl)). It also checks the pip invocation in the build script (see [pip flags](#pip-flags)). It then compares two sets of requirements:

- **Run requirements:** `Requires-Dist` and `Requires-Python` from `PKG-INFO` (not `pyproject.toml`, which can use dynamic metadata) against `requirements.run` and `context.python_min`. Any dependency only in the recipe, only in the sdist, or with different bounds is a mismatch.
- **Host requirements:** `[build-system].requires` from the sdist's `pyproject.toml` against `requirements.host`. Only a build requirement missing from host is a mismatch. A renamed or replaced build tool shows up here, e.g. upstream `versioneer` vs. recipe `versioneer-518`. **Don't add version bounds to host requirements**, because conda-forge avoids them. Upstream build-tool bounds that the host entries lack (e.g. `setuptools>=61.0`, `hatchling>=1.27,<1.32`) are printed as `info` only, so leave them out. `wheel` is ignored, and host-only entries such as `pip` are informational.
- **Implied dependencies:** conda-forge's `jax` always controls `jaxlib`, so **never add `jaxlib`** to a recipe, even when upstream metadata lists it. The script leaves a sdist `jaxlib` out of the comparison and prints an `info` line. If upstream raises the `jaxlib` floor, raise the `jax` floor instead if it doesn't already cover it. An existing `jaxlib` entry in a recipe is also reported as redundant `info`; leave it unless asked. To add more pairs like this, extend `IMPLIED_BY` in the script.

It maps common PyPI→conda names (`matplotlib`→`matplotlib-base`), normalizes `_`/`-`, and exits 1 on any mismatch. Pass `--pypi-name` if the PyPI name differs from `package.name`.

For anything it reports under `REVIEW`, decide by hand:
- Dependencies conditional on environment markers: e.g. `tomli; python_version < '3.11'` should be dropped when `python_min` is at least 3.11.
- `if:` blocks in the recipe are not compared.
- Build requirements with extras: e.g. `versioneer[toml]` needs the extra's own dependencies in host, because the conda package may not pull them in. For `versioneer[toml]` with a host Python below 3.11 that means adding `tomli` to host.
- No `pyproject.toml` in the sdist: check `setup.py`/`setup.cfg` build requirements by hand.

#### pip flags

The pip invocation in the build script must be only `${{ PYTHON }} -m pip install . -vv` in a v1 `recipe.yaml`, or `{{ PYTHON }} -m pip install . -vv` in a v0 `meta.yaml`, as in the [pure-Python example recipe](https://conda-forge.org/docs/maintainer/example_recipes/pure-python/). Both rattler-build and conda-build now apply `--no-deps` and `--no-build-isolation` to pip themselves, so the script reports either flag as a mismatch. Remove them in a **separate commit** after the bot's commits, e.g. `MNT: Remove redundant pip flags`. Other differences from that line (e.g. `--ignore-installed`, `--no-cache-dir`, or installing a subdirectory) are reported for review: drop flags that only repeat pip defaults or build-tool settings, and keep anything the build needs. The rule applies to any recipe that runs pip, e.g. a Rust package built with maturin, but only `compare_requirements.py` checks it, and only for a v1 `recipe.yaml`. For a v0 `meta.yaml` or another package type, check by hand, e.g. `grep -rn 'pip install' recipe/`, and check a v0 recipe's PyPI `source.url` by hand against [Check `source.url`](#check-sourceurl) too.

### R packages

R feedstocks (`r-<name>`) take their source from CRAN (usually a list of two URLs: `{{ cran_mirror }}/src/contrib/<name>_<version>.tar.gz` and the `src/contrib/Archive/<name>/` fallback) or from a GitHub tag. Their dependency metadata is the package's `DESCRIPTION` file, not PyPI. Run from the feedstock root:

```bash
pixi exec --force-reinstall -s python -s pyyaml -- python "${CLAUDE_SKILL_DIR}"/scripts/compare_r_requirements.py recipe/recipe.yaml   # or recipe/meta.yaml
```

It downloads the source from the recipe's own rendered `source.url`, trying each URL in a list. It fills in the conda-forge global `cran_mirror` as `https://cloud.r-project.org`, verifies `source.sha256`, and compares `DESCRIPTION` with the recipe's `r-*` entries using conda-forge R conventions:

- **Run requirements:** `Depends` + `Imports`. The names become `r-<lowercase name>` and bounds like `(>= 1.2)` become `>=1.2`.
- **Host requirements:** `Depends` + `Imports` + `LinkingTo`. `LinkingTo` packages (headers, e.g. `Rcpp`, `cpp11`) belong in host only.
- **Skipped:** base R packages (`utils`, `tools`, `stats`, `methods`, `graphics`, `grDevices`, `grid`, `parallel`, `splines`, `stats4`, `tcltk`, `compiler`, `datasets`) aren't separate conda packages. Compilers, `make` and other non-`r-*` entries are ignored.
- **R version:** `Depends: R (>= x)` is compared with `r-base`. An unpinned `r-base` is conventional, because the `r_base` variants in `.ci_support/` pin it. The script flags it for review, and it's fine as long as every variant satisfies the bound.
- **Compiled code:** a package with a `src/` directory must not be `noarch`. The script flags that as a mismatch.
- **Recipe formats:** v1 `recipe.yaml` is read directly. Most CRAN-generated R feedstocks still use v0 `meta.yaml`, which is read approximately: `{% set %}` variables and `{{ }}` expressions are filled in, and `r-*` list items with a `# [selector]` are set aside and listed for review. An example is `r-later  # [build_platform != target_platform]`, the cross-compilation copy in `build`.

For anything it reports under `REVIEW`, decide by hand:
- `SystemRequirements` (e.g. `pandoc`, `libnng`): these map to non-R conda packages or bundled sources, and the recipe may already handle them through build scripts or `pin_compatible`. Check that the recipe still covers them. They are not compared automatically.
- Conditional `r-*` entries: keep the cross-compilation `build` copies in sync with the host and run changes you make, e.g. remove a `r-later  # [build_platform != target_platform]` line when `later` leaves `Imports`.

`Suggests` are optional in R and don't belong in run requirements. Recipe tests may still need some of them (e.g. `r-testthat`), so compare the test requirements with `Suggests` when a test fails. Also check for a moved dependency: a package that moved from `Imports` to `Suggests` should be removed from host and run.

For a local build of an R feedstock, use one of its variant files, e.g. `.ci_support/linux_64_r_base4.5.yaml`.

### Rust and Go packages

Rust and Go packages have no released metadata that lists conda requirements: their dependencies are compiled in from `Cargo.lock` or `go.mod`, and only native libraries, compilers and build tools appear in the recipe. So these scripts compare the PR's source with the source of the version on `main`, and check the recipe against what changed. Run from the feedstock root:

```bash
pixi exec --force-reinstall -s python -s pyyaml -- python "${CLAUDE_SKILL_DIR}"/scripts/compare_rust_requirements.py recipe/recipe.yaml   # Rust
pixi exec --force-reinstall -s python -s pyyaml -- python "${CLAUDE_SKILL_DIR}"/scripts/compare_go_requirements.py recipe/recipe.yaml     # Go
```

Both download the recipe's main source (the `source` entry whose URL uses the version) and the source of the version in `origin/main`'s recipe (`--base-ref` changes the ref), and verify `source.sha256`. They read both archives in memory and never extract or run them. They then follow the build script as it would run on linux-64: `if:` conditions are evaluated for that platform, `cd`, `pushd` and `popd` are tracked, and `target_directory` is honored, so paths resolve the way the build sees them. A rebuild PR (same version as `main`) skips the comparisons with the base version.

Both scripts check:

- **Licenses:** the third-party license bundle that the build writes (`cargo-bundle-licenses --output <file>` for Rust, `go-licenses save --save_path <dir>` for Go) must be in `about.license_file`. Every `license_file` entry must exist in the source, in another source's `file_name`, or in the recipe directory. Either problem is a mismatch. License files that changed since the base version, a changed crate `license` field, and top-level `LICENSE*`/`COPYING*`/`NOTICE*` files not in `license_file` are reported for review.
- **Compiler version:** the source's minimum (`rust-version` in `Cargo.toml`, including one inherited from the workspace, or the `go` directive in `go.mod`) is compared with `rust_compiler_version`/`go_compiler_version` in `.ci_support/*.yaml`, or, as conda-forge normally leaves these unpinned, with the newest `rust`/`go` on conda-forge. A minimum newer than that is reported for review: no recipe change fixes it, so tell the user that CI will fail until conda-forge's compiler is updated.

The Rust script also checks:

- **Crates and binaries:** each `cargo install --path <dir>` is resolved to its crate. Its binaries come from `[[bin]]` targets and Cargo's auto-discovery (`src/main.rs`, `src/bin/`), or from `--bin`. A binary that a `package_contents` test lists but the crate no longer defines is a mismatch, and so is a new binary that a `strict: true` `package_contents` test doesn't list, because the strict test would fail. A missing `Cargo.lock` is a mismatch when the build uses `--locked`. A crate directory that existed in the base version but is gone is a mismatch.
- **`Cargo.lock` changes:** crates added, removed and changed since the base version.
- **Native code:** new `-sys` crates (other than pure OS bindings such as `windows-sys`) and new build-script helper crates (`cmake`, `pkg-config`, `bindgen`, `cc`). For `-sys` crates of libraries that conda-forge packages (`openssl-sys`, `libgit2-sys`, `libssh2-sys`, `libz-sys`, `zstd-sys`, `libsqlite3-sys`, `bzip2-sys`, `curl-sys`), the review line names the host package and the variable that makes the crate use it instead of building a bundled copy (e.g. `LIBGIT2_NO_VENDOR=1`). `Cargo.lock` lists crates for every feature and target, so a new crate may not be built at all: check the crate's features and the build log before adding host requirements. Changes to the crate's `build.rs` are also reported for review, because a build script can start linking a native library without any `-sys` crate.

The Go script also checks:

- **Packages and binaries:** each local package that `go build`, `go install` or `go run` uses must exist in the source. A package directory that existed in the base version but is gone is a mismatch, because upstream moved it and the build script must follow. Binaries named by `-o` are compared with the `package_contents` tests for review only, since Go names a binary built without `-o` after its directory or module.
- **`go.mod` changes:** requirements added, removed and changed since the base version, a changed `go` directive, and a changed module path (for review).
- **cgo:** files with `import "C"` in the module (and in `vendor/` when dependencies are vendored) are reported for review when the recipe uses `go-nocgo`. That is fine when they sit behind a `cgo` build tag with a pure-Go fallback. Otherwise the recipe needs `${{ compiler('go-cgo') }}` with `${{ compiler('c') }}` and `${{ stdlib('c') }}`. New requirements on known cgo modules (e.g. `github.com/mattn/go-sqlite3`) are reported too. Dependencies are not downloaded, so cgo in an unvendored dependency only shows up in the build.
- A build through `make` or another wrapper has no `go build` to follow. The script then compares the top-level `go.mod` only and says that package paths and binaries were not checked; read the `Makefile` by hand for moved packages.

Only v1 `recipe.yaml` recipes with a single output are supported; anything else exits with `RESULT: ERROR`, so compare by hand. Git and local-path sources are not supported either. Additional `url` sources without a `file_name` are not downloaded and are listed for review.

Fix each kind of mismatch in its own commit after the bot's commits (see [step 4](#4-requirements-commit-only-if-needed)): new host libraries, `pkg-config` and vendoring variables in `MNT: Update to v<version> requirements`, and a third-party license bundle missing from `about.license_file` in `MNT: Package third-party licenses`. For a moved package path or a renamed binary, update the build script and tests in a commit that says what moved upstream. Confirm all of these with a local build.

When a new dependency or raised floor appears, check that conda-forge can satisfy it (`pixi search -c conda-forge <name>`). If it can't, still make the recipe match the metadata, and tell the user that CI will fail until that version is available. Do not act on the dependency's own feedstock.

If the PR already has CI results, look at any failure before editing (`pixi exec --force-reinstall cf-job-logs --gh list-jobs --json <PR_URL>`, then `download-log`), so the fix covers the actual error and not just the metadata diff.

Also check whether the upstream source still uses anything the metadata dropped (for example, grep the extracted sdist for imports of a removed package), and whether `tests.python.imports` or `build.python.entry_points` reference modules or entry points that no longer exist (compare `[project.scripts]` and the package list in `pyproject.toml` between the old and new sdists).

## 4. Requirements commit (only if needed)

If validation found differences, fix them in `recipe/recipe.yaml` as a **separate commit** after the bot's version commit, never folded into it:

```bash
git add recipe/recipe.yaml
git commit -m "MNT: Update to v<version> requirements" -m "Assisted-by: ClaudeCode:<model-id>"
```

Host and run changes can go in the same commit; when the reason isn't obvious (e.g. a build-backend or versioneer change), explain it in the commit body. Match the repository's style from step 1 if it differs. Re-run the validation script until it reports `RESULT: MATCH`.

If host requirements changed, a Rust or Go build script or `license_file` changed, or the PR's CI was failing at the build step, confirm the fix with a local build before pushing:

```bash
pixi exec --force-reinstall rattler-build build --recipe recipe -m .ci_support/linux_64_.yaml --output-dir "$(mktemp -d -p <scratchpad>)"
```

Check that it ends with `all tests passed!` and that the installed version is the real one (e.g. `Successfully installed <name>-<version>`), not `0+unknown` or `0.0.0`.

## 5. Regenerate

**Invariant:** if the PR contains a regeneration commit (`MNT: Re-rendered with conda-smithy ...`), it must be the last commit on the branch, and there must be only one. A "No changes made" result from `conda-smithy regenerate -c` does **not** mean the invariant holds: when an earlier regeneration commit is still in the PR history (common when the PR was already based on `main`, so the rebase had nothing to drop), regenerating after a later requirements commit reports "No changes" because the earlier commit already produced identical files, and that commit stays in the middle of the history.

So, before regenerating, check whether any regeneration commit survived in `origin/main..HEAD`:

```bash
git log --format='%h %s' origin/main..HEAD | grep 'MNT: Re-rendered with conda-smithy'
```

- **None survived:** run `conda-smithy regenerate -c` once. If it makes a commit, it is last by construction.
- **One survived and it is already the last commit:** run `conda-smithy regenerate -c`. If it reports "No changes", you are done. If it makes a new commit, drop both regeneration commits and regenerate again (see the next bullet).
- **One survived and it is not the last commit** (e.g. you added a requirements commit after it): rebuild the branch without it and regenerate, even if a regeneration would report "No changes". `git rebase -i` is unavailable, so rebuild with cherry-pick:

  ```bash
  git switch -C pr<N> origin/main
  git cherry-pick <every non-regeneration commit from the PR, oldest first>
  conda-smithy regenerate -c
  ```

  After this, `conda-smithy regenerate -c` will make a fresh commit, because the rendered files on `main` are older. If it reports "No changes", `main`'s rendered files already match and no regeneration commit is needed at all.

Every regeneration commit conda-smithy makes lacks the `Assisted-by` trailer, so amend it on (`git commit --amend --no-edit --trailer "Assisted-by: ClaudeCode:<model-id>"`).

Final check (both must pass before pushing):

```bash
git log --format='%h %an: %s' origin/main..HEAD   # any 'MNT: Re-rendered' commit must be the single, top-most one
git status --short                                # must be clean
git grep -n "<name>-feedstock" -- pixi.toml README.md | head -3   # the feedstock name must be the real one
```

Read the full diff of the regeneration commit (`git show HEAD`) before pushing. It must contain only changes that follow from the recipe, `conda-forge.yml`, and pinning changes. If it changes the feedstock name or touches files the inputs don't explain, stop and find the cause. Never fix a generated file by hand.

When rebuilding, compare the new tree with the old PR head (`git diff --stat <old headRefOid> HEAD`). It should be empty or contain only the intended changes, which confirms that only the commit order changed.

## 6. Push

Push with a lease pinned to the PR's original head, so nothing pushed to the branch in the meantime is overwritten:

```bash
git push --force-with-lease=<head-branch>:<old headRefOid> bot pr<N>:<head-branch>
gh pr view <N> -R conda-forge/<feedstock> --json headRefOid,mergeable
```

The new `headRefOid` may take a few seconds to update on GitHub, so re-query if it still shows the old one.

If the push is rejected with `(stale info)`, someone pushed to the PR branch after step 1, and the lease kept their commits from being overwritten. Stop: don't retry with `--force` or with a lease on the new head. Show the user what was pushed (`git fetch bot <head-branch>`, then `git log --format='%h %an: %s' <old headRefOid>..bot/<head-branch>`) and let them decide how to continue.

## 7. Mark the PR checklist

The bot's PR description has a "Checklist before merging this PR" with three items. After a successful run, mark the ones that this skill confirms:

- `Dependencies have been updated if changed`: tick it once the validation in step 3 reports `RESULT: MATCH` (after any requirements commit), or, when the script could not run, once every requirement was compared by hand.
- `Tests have passed`: tick it once all CI checks on the PR's final head have passed (`gh pr checks <N> -R conda-forge/<feedstock>`). After a push, CI runs again, so wait for it to finish. If it fails, or is still running when you report, leave the box unticked and say so.
- `Updated license if changed and license_file is packaged`: only for Rust and Go packages, whose scripts check licenses. Tick it once the script's `licenses` section reports no `MISMATCH` (after any license commit) and every `REVIEW` line in it was resolved, e.g. a changed license file was read and `about.license` still matches it. For Python and R packages, leave it alone unless the user asks; the skill doesn't check their licenses.

Drop any `sed` expression for an item that isn't confirmed. Edit only those checkboxes, and keep the rest of the body unchanged:

```bash
gh pr view <N> -R conda-forge/<feedstock> --json body -q .body > <scratchpad>/pr<N>-body.md
sed -i -e 's/^- \[ \] Dependencies have been updated/- [x] Dependencies have been updated/' \
       -e 's/^- \[ \] Tests have passed/- [x] Tests have passed/' <scratchpad>/pr<N>-body.md
# Rust and Go only, when the license check passed:
sed -i 's/^- \[ \] Updated license if changed/- [x] Updated license if changed/' <scratchpad>/pr<N>-body.md
gh pr edit <N> -R conda-forge/<feedstock> --body-file <scratchpad>/pr<N>-body.md
```

If the edit is refused (e.g. the account can't edit the PR), report which items are confirmed so the user can tick them by hand.

## 8. Report

Tell the user:
- the new PR head SHA and whether GitHub reports it mergeable
- the commits now on the PR (on top of `main`), and which bot commits were dropped and why
- how conflicts were resolved, and that the sha256 was verified
- for rebuild PRs, the PR's and `main`'s build numbers, and whether the build number had to be restored
- whether the bot changed `source.url`, and whether the URL was changed to the `files.pythonhosted.org/packages/source` form
- for Python recipes, whether redundant pip flags were removed from the build script
- the validation result for run and host requirements: what mismatched, what the requirements commit changed, and any new floors that conda-forge cannot yet satisfy
- for Rust and Go packages, the `Cargo.lock`/`go.mod` changes in brief, any new native or cgo dependencies and how they were resolved, the compiler-version check, the license check and any license commit, and the `REVIEW` lines left for the user
- the local build result, if one was run, and the cause of any CI failure found on the PR
- the regeneration outcome, including whether an older regeneration commit was dropped so the regeneration commit is last
- which PR checklist items were ticked, and why any were left unticked
- the absolute path of the working clone and branch
