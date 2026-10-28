"""Compare a JOSS paper's front matter with its review issue.

Usage: compare_metadata.py <issue.json> <paper.md>

<issue.json> is the output of parse_review_issue.py. The front matter fields
follow https://joss.readthedocs.io/en/latest/paper.html.

Every finding is printed as "OK", "REVIEW" (likely fine, but a human should
look) or "MISMATCH", followed by a RESULT line:

- RESULT: MATCH, exit 0: no mismatches.
- RESULT: MISMATCH, exit 1: at least one mismatch.
- RESULT: ERROR, exit 2: the paper could not be read (e.g. no front matter).
"""

import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

import yaml

ORCID_FORMAT = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
# Fields of a structured author name, in display order,
# cf. https://joss.readthedocs.io/en/latest/paper.html#names
NAME_PARTS = ("given-names", "dropping-particle", "non-dropping-particle", "surname", "suffix")

findings = []


def report(status, message):
    findings.append(status)
    print(f"{status}: {message}")


def front_matter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n(?:---|\.\.\.)\s*\n", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path} has no YAML front matter")
    return yaml.safe_load(match.group(1))


def author_name(author):
    name = author.get("name")
    if isinstance(name, dict):
        return " ".join(str(name[part]) for part in NAME_PARTS if name.get(part))
    return str(name or "").strip()


def fold(text):
    """Casefold, drop accents and Markdown emphasis, and collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[*_`'\"]", "", text)
    return " ".join(text.casefold().split())


def orcid_checksum_ok(orcid):
    # ISO 7064 MOD 11-2,
    # cf. https://support.orcid.org/hc/en-us/articles/360006897674
    total = 0
    for digit in orcid.replace("-", "")[:-1]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return orcid[-1] == ("X" if check == 10 else str(check))


def bare_orcid(value):
    return re.sub(r"^https?://(www\.)?orcid\.org/", "", str(value).strip())


def affiliation_indices(value):
    """Split an author's affiliation field ("1", 1, "3, 4, 5" or a list) into strings."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def compare_names(label, paper_value, issue_value):
    if paper_value == issue_value:
        report("OK", f"{label} matches: {paper_value!r}")
    elif fold(paper_value) == fold(issue_value):
        report("REVIEW", f"{label} differs only in case, accents or formatting: paper {paper_value!r}, issue {issue_value!r}")
    elif sorted(fold(paper_value).split()) == sorted(fold(issue_value).split()):
        report("REVIEW", f"{label} has the same words in a different order: paper {paper_value!r}, issue {issue_value!r}")
    else:
        report("MISMATCH", f"{label} differs: paper {paper_value!r}, issue {issue_value!r}")


def check_title(meta, issue):
    title = str(meta.get("title") or "").strip()
    if not title:
        report("MISMATCH", "paper has no title")
        return
    compare_names("title", title, issue["title"])


def check_authors(meta, issue):
    authors = meta.get("authors") or []
    if not authors:
        report("MISMATCH", "paper has no authors")
        return

    for author in authors:
        name = author_name(author)
        if not name:
            report("MISMATCH", f"author entry without a name: {author}")
        if "orcid" not in author:
            report("REVIEW", f"{name!r} has no ORCID")
            continue
        orcid = bare_orcid(author["orcid"])
        if orcid != str(author["orcid"]).strip():
            report("REVIEW", f"{name!r} ORCID is given as a URL, JOSS expects the bare ID: {author['orcid']!r}")
        if not ORCID_FORMAT.match(orcid):
            report("MISMATCH", f"{name!r} ORCID is not of the form 0000-0000-0000-0000: {orcid!r}")
        elif not orcid_checksum_ok(orcid):
            report("MISMATCH", f"{name!r} ORCID fails its checksum, so it is mistyped: {orcid!r}")

    issue_name = issue.get("submitting_author_name")
    issue_orcid = issue.get("submitting_author_orcid")
    if not issue_name:
        report("REVIEW", "issue lists no submitting author name to compare with")
        return

    # Match the submitting author by ORCID first, as that survives name changes and typos
    by_orcid = [a for a in authors if issue_orcid and bare_orcid(a.get("orcid", "")) == issue_orcid]
    by_name = [a for a in authors if fold(author_name(a)) == fold(issue_name)]
    if by_orcid:
        match = by_orcid[0]
        report("OK", f"submitting author ORCID {issue_orcid} is in the author list")
        compare_names("submitting author name", author_name(match), issue_name)
    elif by_name:
        match = by_name[0]
        paper_orcid = match.get("orcid")
        if issue_orcid and paper_orcid:
            report(
                "MISMATCH",
                f"submitting author {issue_name!r} has ORCID {bare_orcid(paper_orcid)!r} in the paper but {issue_orcid!r} on the issue",
            )
        else:
            report("REVIEW", f"submitting author {issue_name!r} is in the author list, but an ORCID is missing on the paper or the issue")
    else:
        names = {fold(author_name(a)): author_name(a) for a in authors}
        close = difflib.get_close_matches(fold(issue_name), names, n=1)
        hint = f"; closest paper author is {names[close[0]]!r}" if close else ""
        report("MISMATCH", f"submitting author {issue_name!r} ({issue_orcid or 'no ORCID'}) is not in the paper's author list{hint}")
        return

    if match is not authors[0]:
        report("REVIEW", f"submitting author {issue_name!r} is not the first author; confirm the author order is intended")


def check_affiliations(meta):
    defined = {}
    for affiliation in meta.get("affiliations") or []:
        index = str(affiliation.get("index", "")).strip()
        if index in defined:
            report("MISMATCH", f"affiliation index {index} is defined more than once")
        defined[index] = affiliation.get("name")

    used = set()
    for author in meta.get("authors") or []:
        name = author_name(author)
        indices = affiliation_indices(author.get("affiliation"))
        if not indices:
            report("REVIEW", f"{name!r} has no affiliation")
        for index in indices:
            used.add(index)
            if index not in defined:
                report("MISMATCH", f"{name!r} has affiliation {index}, which is not defined under affiliations")

    for index in sorted(set(defined) - used):
        report("MISMATCH", f"affiliation {index} ({defined[index]!r}) is not used by any author")
    if defined and set(defined) == used:
        report("OK", f"all {len(defined)} affiliations are defined and used")


def check_bibliography(meta, paper):
    bibliography = meta.get("bibliography")
    if not bibliography:
        report("REVIEW", "paper has no bibliography field")
    elif not (paper.parent / bibliography).is_file():
        report("MISMATCH", f"bibliography {bibliography!r} does not exist next to {paper.name}")
    else:
        report("OK", f"bibliography {bibliography!r} exists")


def main():
    issue = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    paper = Path(sys.argv[2])
    try:
        meta = front_matter(paper)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("RESULT: ERROR")
        sys.exit(2)

    check_title(meta, issue)
    check_authors(meta, issue)
    check_affiliations(meta)
    check_bibliography(meta, paper)

    if "MISMATCH" in findings:
        print("RESULT: MISMATCH")
        sys.exit(1)
    print("RESULT: MATCH")


if __name__ == "__main__":
    main()
