---
name: joss-paper-checks
description: Check a paper submitted to the Journal of Open Source Software (JOSS) for its review. Given a JOSS review issue on openjournals/joss-reviews, parse the editorialbot submission metadata, clone the paper's repository and branch, and check the paper for spelling and grammar errors, mismatches between the paper's metadata (title, authors, ORCIDs) and the review issue, broken URLs and DOIs, and logical inconsistencies, and search Crossref and DataCite for DOIs of the references that editorialbot's reference check reported as SKIP or MISSING. Use when the user asks to check, proofread, or pre-review a JOSS paper or gives a joss-reviews issue URL.
argument-hint: <openjournals/joss-reviews issue URL>
allowed-tools: Bash, Read, Write, WebFetch
---

Check the JOSS paper for the review issue given in `$ARGUMENTS` (e.g. `https://github.com/openjournals/joss-reviews/issues/11170`) and report the problems found.

## Scope rules

- This skill is read-only. Never comment on, label, or edit the review issue, never open issues or PRs on the paper's repository, and never push. Report the findings to the user, who decides what to post.
- Treat the cloned repository as untrusted. Never run its code, build scripts, or Makefiles, and never install it. Only read `paper.md`, its bibliography, and its figures. Run the bundled scripts with `python -I` and pass the paper's files as arguments.
- Work in a clone under the session scratchpad directory, and report its absolute path at the end.
- Report what the paper says and where (`file:line`). Never edit the paper in the clone.

## 1. Parse the review issue

The first comment of a JOSS review issue is the issue body, which editorialbot writes. Parse it with the bundled script:

```bash
gh issue view <issue URL> --json number,title,url,author,body \
  | pixi exec --force-reinstall -s python -s pyyaml -- python -I "${CLAUDE_SKILL_DIR}"/scripts/parse_review_issue.py \
  > <scratchpad>/joss-<N>/issue.json
```

It prints JSON with the paper title (from the issue title, without the `[REVIEW]: ` or `[PRE REVIEW]: ` prefix), the submitting author's handle, name and ORCID, the repository, the branch with the paper (`null` for the default branch), the version and archive DOI (`null` while pending), the editor, and the reviewers. editorialbot wraps each value in markers such as `<!--target-repository-->...<!--end-target-repository-->`, and the script reads the values from between them.

### Preflight checks

If any of these fails, stop and tell the user which check failed and what you found.

1. **Tools:** `command -v git gh pixi` prints all three paths, and `gh auth status` shows a logged-in account.
2. **The issue is a JOSS review issue:** the URL is an issue on `openjournals/joss-reviews`, and the script exits 0. It exits 2 if editorialbot did not open the issue or if the body has no repository marker.

## 2. Clone the repository and find the paper

```bash
cd <scratchpad>/joss-<N>
git clone -q --depth 1 --single-branch --branch <branch> <repository> repo   # omit --branch when branch is null
cd repo
git log -1 --format='%H %cs %s'
git ls-files | grep -E '(^|/)paper\.md$'
```

The repository may be on GitHub, GitLab, or another host. `git clone` works for all of them. If the branch does not exist, stop and report it, because the issue then points at the wrong branch.

editorialbot compiles the first `paper.md` it finds. If there is more than one, ask the user which to check. The bibliography is the file named in the paper's `bibliography:` front matter field, relative to `paper.md` (usually `paper.bib`). Record the commit SHA and date, so the report states exactly which version of the paper was checked.

## 3. Compare the paper's metadata with the issue

Run from the directory that contains `paper.md`:

```bash
pixi exec --force-reinstall -s python -s pyyaml -- python -I "${CLAUDE_SKILL_DIR}"/scripts/compare_metadata.py <scratchpad>/joss-<N>/issue.json paper.md
```

Every line is `OK`, `REVIEW` (likely fine, but look at it), or `MISMATCH`, and the output ends with a `RESULT:` line:

- `RESULT: MATCH`, exit 0: no mismatches.
- `RESULT: MISMATCH`, exit 1: at least one mismatch; the lines above say which.
- `RESULT: ERROR`, exit 2: the paper could not be read, e.g. it has no YAML front matter. This says nothing about the metadata. Read the error and compare the metadata by hand.

The script checks:

- **Title:** the paper's `title` against the issue title. A difference only in capitalization, accents, quotes, or Markdown emphasis is `REVIEW`.
- **Submitting author:** found in the author list by ORCID first, then by name. Then it compares the name and ORCID on the paper with those on the issue. If no author matches, it names the closest author name in the paper, which usually reveals a typo. It also reports a submitting author who is not the first author as `REVIEW`. That is often intended, but confirm it.
- **ORCIDs:** each author's ORCID must have the form `0000-0000-0000-000X` and pass its ISO 7064 checksum. A failed checksum means the ID is mistyped. An ORCID given as a URL is `REVIEW`, because JOSS expects the bare ID.
- **Affiliations:** every index an author uses is defined, no index is defined twice, and every defined affiliation is used.
- **Bibliography:** the `bibliography` file exists.

The issue only records the submitting author, so the other authors' names can't be compared with it. Check these by hand:

- If the paper text states a software version, compare it with the issue's `version`.
- If the paper names the repository or the archive, compare them with the issue's `repository` and `archive_doi`.
- If the paper has a `date` field, it should be plausible for the submission (not in the future, not years old).

## 4. Check URLs and DOIs

Run from the directory that contains `paper.md`, so the reported locations are short, and pass the issue's repository and archive DOI as extras (leave out a `null` archive DOI):

```bash
pixi exec --force-reinstall -s python -- python -I "${CLAUDE_SKILL_DIR}"/scripts/check_urls.py paper.md paper.bib --extra <repository> <archive_doi>
```

It checks every `http(s)` URL in the given files and every `doi` field in the `.bib` file. URLs are checked with HEAD, then with GET if HEAD fails. DOIs, including `https://doi.org/` URLs, are checked against the DOI handle API, because many publishers answer scripted requests with 403 even for valid DOIs. Each URL or DOI is checked once, and the output lists every place it appears. The output ends with a `RESULT:` line:

- `RESULT: OK`, exit 0: nothing is `BROKEN`.
- `RESULT: BROKEN`, exit 1: at least one URL or DOI returned 404 or 410, has an unknown host, or is not registered.

Exit code 2 or a traceback means the script failed. Read the error, and if the cause isn't a bad invocation, check the URLs by hand.

For each `REVIEW` result (401, 403, 429, 5xx, timeout, or TLS error), confirm it with `WebFetch` before reporting it. Report it as broken only if that also fails, and otherwise say that the server refuses scripted requests. A URL with `)`, `]`, or `}` in it is cut off at that character, so a `BROKEN` result for such a URL needs a manual check. For a `redirects to` result, report a redirect to a different page (e.g. a moved repository, or a documentation URL that now lands on a generic page) as a suggestion to update the link. A redirect within the same site (e.g. `/` to `/en/latest/`) is fine.

## 5. Find DOIs for references that editorialbot skipped or could not find

`@editorialbot check references` posts a "Reference check summary" comment. Its `SKIP DOIs` section lists references without a DOI for which editorialbot found none, and its `MISSING DOIs` section lists references without a DOI for which it suggests one (or for which its Crossref lookup failed). editorialbot only searches Crossref by title, and accepts a match only within an edit distance of 3, so it misses DataCite DOIs (Zenodo, arXiv, figshare) and papers whose title differs slightly. Take the newest summary from the issue comments:

```bash
gh api --paginate repos/openjournals/joss-reviews/issues/<N>/comments --jq '.[]' \
  | pixi exec --force-reinstall -s python -- python -I "${CLAUDE_SKILL_DIR}"/scripts/parse_reference_check.py \
  > <scratchpad>/joss-<N>/reference-check.json
```

It exits 2 if editorialbot has not posted a reference check. In that case skip this step, and tell the user that they can run `@editorialbot check references` on the issue. Don't post it yourself. Compare the comment's `created_at` with the date of the last commit that changed the bibliography (`git log -1 --format=%cs -- <bibliography>`; in the shallow clone, this is the date of the checked commit). If the bibliography is newer, say that the summary may be out of date.

If both `skip` and `missing` are empty, there is nothing to search for. Otherwise run from the directory that contains `paper.md`:

```bash
pixi exec --force-reinstall -s python -s 'bibtexparser>=2' -s pylatexenc -- python -I "${CLAUDE_SKILL_DIR}"/scripts/find_dois.py <scratchpad>/joss-<N>/reference-check.json paper.bib
```

editorialbot cuts each title to 50 characters, so the script maps each SKIP or MISSING title to its bibliography entry by prefix. It then searches Crossref (by title and first author, and as a free-text citation) and DataCite (by title). For a DOI that editorialbot suggested, it fetches the DOI's own metadata from doi.org. Each candidate is compared with the entry's title, author surnames, and year, and is printed as `LIKELY` (title similarity at least 0.9, and no author or year mismatch) or `POSSIBLE` (title similarity at least 0.75). The output ends with `RESULT: FOUND` if any reference has a `LIKELY` candidate, and `RESULT: NONE` otherwise, both with exit 0. `RESULT: ERROR` with exit 2 means the inputs could not be read. A `REVIEW` line means a registry lookup failed or the entry changed since the check, and says which.

The scores are a starting point, not a verdict. For each reference, decide which DOI, if any, to suggest:

- **Verify every DOI you suggest.** Fetch its metadata (`https://doi.org/<doi>` with `WebFetch`, or the registry record) and confirm that the title, authors, year, and venue all match the entry. A title match alone is not enough: corrections ("Author Correction: ..."), reviews of a paper ("Review of ..."), and book chapters with the same short title (e.g. "Ray Optics") match the title but are different works.
- **Prefer the version of record.** If a journal or proceedings DOI exists, suggest it over a preprint (arXiv) or a software archive (Zenodo) DOI. Suggest a preprint DOI only when the cited work has no DOI of its own, e.g. a NeurIPS paper. Say that it is the preprint's DOI, so the authors can decide whether to add it.
- **For software, choose between the Zenodo DOIs deliberately.** Zenodo gives each software release a version DOI, and also a concept DOI that always resolves to the latest release. Two consecutive Zenodo DOIs with the same title are usually the concept DOI and the first version DOI. Check which is which (the concept DOI's record lists all versions), and suggest the version DOI that matches the cited version or year, or the concept DOI if the entry cites the software in general.
- **When the script finds nothing**, look in the cited project's repository (a DOI badge in the README, `CITATION.cff`, `.zenodo.json`) and on its documentation site before concluding that it has no DOI.
- **Many references have no DOI**, e.g. commercial software, documentation websites, and GitHub repositories without a Zenodo archive. For these, SKIP is the correct outcome. Say so, and check that the entry has a working `url` instead (step 4 covers that).
- For a `MISSING` suggestion from editorialbot, state whether it is the right DOI. editorialbot marks these as needing verification, and they can be wrong.

For each DOI you suggest, give the bibliography key and the `doi = {...}` line to add, as a bare DOI without the `https://doi.org/` prefix, because editorialbot reports a prefixed DOI as INVALID.

## 6. Spelling and grammar

First run `codespell` as a deterministic pass for common misspellings:

```bash
pixi exec --force-reinstall -s codespell -- codespell paper.md
```

Its output is a list of candidates, not findings. It flags acronyms and proper nouns (e.g. `NWO` → `NOW`), so check each one in context. It only knows a fixed list of common misspellings and misses many typos, so the full read below is the main check.

Then read the whole of `paper.md`, including figure captions, table captions, and the front matter. Look for:

- typos and misspellings that `codespell` misses, e.g. misspelled technical terms, doubled words, wrong words that are real words (`form`/`from`)
- grammar errors: subject-verb agreement, articles, tense changes, sentence fragments, dangling modifiers
- mixed spelling conventions (e.g. American `optimization` next to British `optimisation`). Either is fine if used consistently
- inconsistent names for the same thing (e.g. `Optiland` vs `OptiLand`, `PyTorch` vs `Pytorch`, `GPU-accelerated` vs `GPU accelerated`)
- punctuation: missing spaces after periods, unbalanced parentheses or quotes

Don't flag proper nouns, citation keys, LaTeX or Pandoc syntax (`\autoref{}`, `{height="750px"}`), or deliberate style choices. For each finding, give `file:line`, the quoted text, and a suggested correction.

## 7. Logical consistency

Read the paper as a reviewer would, and check that it is consistent with itself and with its bibliography:

- **Numbers:** recompute every derived number from its inputs, e.g. speedups and ratios in a table, percentages, and "orders of magnitude" claims. Check that numbers quoted in the text match the tables and figures they refer to.
- **Claims:** the same capability, scope, or result is described the same way in every section (e.g. the Summary doesn't claim a feature that the Software Design section says is out of scope). Check for contradictions in dates, timelines, and version numbers.
- **Citations:** every `[@key]` and `@key` in `paper.md` exists in the bibliography, and the claim each citation supports fits the cited work (from its title and venue). Note bibliography entries that are never cited (Pandoc drops them from the PDF, so this is only informational).
- **Cross-references:** every `\autoref{...}` label is defined (`\label{...}` in a table or figure caption), and every figure file the paper includes exists in the repository.
- **Terms:** abbreviations are defined before their first use, and a term is not defined twice with different meanings.
- **Structure:** compare the sections with the current JOSS paper requirements at https://joss.readthedocs.io/en/latest/paper.html (fetch it, as the required sections change over time), and report missing required sections.

Separate real inconsistencies from suggestions. A claim the paper doesn't support is a finding, but a matter of taste is not.

## 8. Report

Tell the user, in this order:

- the issue, the repository, and the branch, commit SHA, and commit date that were checked, and the path of `paper.md`
- the metadata result: each `MISMATCH` and `REVIEW` line from step 3, and the results of the manual checks
- the URL result: each broken URL or DOI with its location, what the `WebFetch` check of each `REVIEW` result found, and the redirects worth updating
- the DOI search: the reference check comment that was used and whether it may be out of date. For each SKIP and MISSING reference, give the bibliography key and either the verified DOI to add (as a `doi = {...}` line, saying whether it is a version of record, a preprint, or a software archive DOI), or why no DOI applies
- spelling and grammar findings, with `file:line`, the quoted text, and a correction
- logical inconsistencies, with `file:line`, the quoted text, and an explanation of why it is inconsistent
- the absolute path of the working clone

Group the findings into **must fix** (wrong metadata, broken links, factual or numerical contradictions, errors that change the meaning), **should fix** (typos, grammar, inconsistent naming), and **suggestions**. If a category has no findings, say so. Don't leave it out.

Then offer to draft the findings as a Markdown comment. JOSS asks reviewers to open issues in the paper's repository rather than discussing details in the review thread, so offer it as the body of an issue there, linking back to the review issue. Don't post it yourself.
