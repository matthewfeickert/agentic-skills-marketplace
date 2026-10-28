"""Extract the submission metadata from a JOSS review issue.

Reads the JSON printed by
`gh issue view <url> --json number,title,url,author,body` on stdin and
prints the fields that editorialbot records in the issue body as JSON.

editorialbot wraps each value in HTML comment markers, e.g.
`<!--target-repository-->https://github.com/org/repo<!--end-target-repository-->`,
so the values are read from between the markers instead of from the
surrounding Markdown, which changes between JOSS versions.

Exit codes: 0 on success, 2 if the issue is not an editorialbot review issue.
"""

import json
import re
import sys

EDITORIALBOT = "editorialbot"
# Issue titles are "[REVIEW]: <paper title>" or "[PRE REVIEW]: <paper title>"
TITLE_PREFIX = re.compile(r"^\s*\[(?:PRE[ -]?)?REVIEW\]:\s*", re.IGNORECASE)
# Values editorialbot uses before an archive DOI or version is set
PLACEHOLDERS = {"", "pending", "none"}


def marker(body, name):
    """Return the text between <!--name--> and <!--end-name-->, or None.

    HTML tags are removed, as some values (e.g. the archive DOI) can be
    rendered as links.
    """
    match = re.search(rf"<!--{name}-->(.*?)<!--end-{name}-->", body, re.DOTALL)
    return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else None


def value(body, name):
    """Return the marker's value, or None if it is unset or a placeholder such as "Pending"."""
    text = marker(body, name)
    return None if (text or "").casefold() in PLACEHOLDERS else text


def first_handle(body, name):
    """Return the first GitHub handle in the marker's value, or None."""
    return (handles(marker(body, name)) or [None])[0]


def handles(text):
    """Return the GitHub handles in a marker value, without the leading @."""
    return re.findall(r"@([A-Za-z0-9-]+)", text or "")


def main():
    issue = json.load(sys.stdin)
    body = issue["body"]

    author = issue["author"]["login"]
    if author != EDITORIALBOT:
        print(f"ERROR: issue was opened by {author!r}, not {EDITORIALBOT!r}", file=sys.stderr)
        sys.exit(2)

    repository = marker(body, "target-repository")
    if not repository:
        print("ERROR: no <!--target-repository--> marker in the issue body", file=sys.stderr)
        sys.exit(2)

    # The submitting author line is
    # "<!--author-handle-->@handle<!--end-author-handle--> (<a href="http://orcid.org/<id>">Name</a>)"
    # and the ORCID link is absent when the author has no ORCID on their JOSS profile.
    author_line = re.search(r"<!--end-author-handle-->\s*\((.*?)\)\s*$", body, re.MULTILINE)
    author_name = author_orcid = None
    if author_line:
        link = re.search(r'<a href="https?://orcid\.org/([0-9X-]+)">(.*?)</a>', author_line.group(1))
        if link:
            author_orcid, author_name = link.group(1), link.group(2).strip()
        else:
            author_name = author_line.group(1).strip()

    result = {
        "issue_number": issue["number"],
        "issue_url": issue["url"],
        "title": TITLE_PREFIX.sub("", issue["title"]).strip(),
        "submitting_author_handle": first_handle(body, "author-handle"),
        "submitting_author_name": author_name,
        "submitting_author_orcid": author_orcid,
        "repository": repository.removesuffix(".git").rstrip("/"),
        # An empty branch means the paper is on the default branch
        "branch": marker(body, "branch") or None,
        "version": value(body, "version"),
        "archive_doi": value(body, "archive"),
        "editor": first_handle(body, "editor"),
        "reviewers": handles(marker(body, "reviewers-list")),
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
