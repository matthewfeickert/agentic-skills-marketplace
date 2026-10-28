"""Extract the latest `@editorialbot check references` result from a JOSS review issue.

Reads the issue comments as JSON lines, as printed by
`gh api --paginate repos/openjournals/joss-reviews/issues/<N>/comments --jq '.[]'`,
on stdin and prints the newest reference check summary as JSON.

The message formats are those of DOIChecker in editorialbot
(https://github.com/openjournals/buffy/blob/main/app/lib/doi_checker.rb).
Titles in the summary are cut to their first 50 characters, which is marked
with "truncated": true.

Exit codes: 0 on success, 2 if the issue has no reference check comment.
"""

import json
import re
import sys

EDITORIALBOT = "editorialbot"
SUMMARY = "Reference check summary"
SECTION = re.compile(r"^\W*\s*(OK|SKIP|MISSING|INVALID) DOIs\s*$")
ITEM_FORMATS = (
    ("skip", re.compile(r"^No DOI given, and none found for title: (?P<title>.*)$")),
    ("suggestion", re.compile(r"^(?P<doi>\S+) may be a valid DOI for title: (?P<title>.*)$")),
    ("crossref_error", re.compile(r'^Errored finding suggestions for "(?P<title>.*)", please try later$')),
)


def parse_item(text):
    """Return a dict describing one summary line, keyed by the kind of message."""
    for kind, pattern in ITEM_FORMATS:
        match = pattern.match(text)
        if match:
            item = {"kind": kind, "message": text, **match.groupdict()}
            if "title" in item:
                item["truncated"] = item["title"].endswith("...")
                item["title"] = item["title"].removesuffix("...")
            return item
    # e.g. "Entry without DOI or title found" or the 10.5555 handle special case
    return {"kind": "other", "message": text}


def parse_summary(body):
    sections = {"ok": [], "skip": [], "missing": [], "invalid": []}
    current = None
    for line in body.splitlines():
        header = SECTION.match(line)
        if header:
            current = sections[header.group(1).lower()]
        elif current is not None and line.startswith("- "):
            text = line[2:].strip()
            if text != "None":
                current.append(text)
    return {
        "ok": sections["ok"],
        "skip": [parse_item(text) for text in sections["skip"]],
        "missing": [parse_item(text) for text in sections["missing"]],
        "invalid": sections["invalid"],
    }


def main():
    comments = [json.loads(line) for line in sys.stdin if line.strip()]
    checks = [c for c in comments if c["user"]["login"] == EDITORIALBOT and SUMMARY in c["body"]]
    if not checks:
        print("ERROR: no editorialbot reference check summary in the issue comments", file=sys.stderr)
        sys.exit(2)

    latest = max(checks, key=lambda c: c["created_at"])
    result = {"comment_url": latest["html_url"], "created_at": latest["created_at"], **parse_summary(latest["body"])}
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
