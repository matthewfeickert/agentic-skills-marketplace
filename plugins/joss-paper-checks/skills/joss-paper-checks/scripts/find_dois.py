"""Search for DOIs for the references editorialbot reported as SKIP or MISSING.

Usage: find_dois.py <reference-check.json> <paper.bib>

<reference-check.json> is the output of parse_reference_check.py. Each SKIP
or MISSING title is matched to its entry in <paper.bib>, which is then looked
up in Crossref (https://api.crossref.org) and DataCite
(https://api.datacite.org). editorialbot only searches Crossref, by title, so
DataCite DOIs (Zenodo, arXiv, figshare, ...) are never suggested by it. For a
DOI that editorialbot suggested as MISSING, the DOI's own metadata is fetched
from doi.org with content negotiation
(https://citation.doi.org/docs.html) and compared with the entry too.

Each candidate is compared with the entry's title, author surnames and year:

- LIKELY: title similarity >= 0.9, and no author or year mismatch.
- POSSIBLE: title similarity >= 0.75.

Weaker candidates are not shown. The output ends with a RESULT line:

- RESULT: FOUND, exit 0: at least one reference has a LIKELY DOI.
- RESULT: NONE, exit 0: no reference has a LIKELY DOI.
- RESULT: ERROR, exit 2: the inputs could not be read.
"""

import difflib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
from pathlib import Path

import bibtexparser
from bibtexparser.middlewares import LatexDecodingMiddleware, SeparateCoAuthors, SplitNameParts

# python -I does not put the script's directory on sys.path, so add it
# explicitly to share the HTTP retry logic with check_urls.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_urls import request

LIKELY, POSSIBLE = 0.9, 0.75
ROWS = 5  # candidates requested from each registry
SHOWN = 3  # candidates printed per reference


def fold(text):
    """Casefold, drop accents, LaTeX braces and commands, and collapse whitespace."""
    text = re.sub(r"\\[a-zA-Z]+\s*|\\.|[{}]", "", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def words(text):
    return re.sub(r"[^\w]+", " ", fold(text)).split()


def load_bib(path):
    library = bibtexparser.parse_file(path, append_middleware=[LatexDecodingMiddleware(), SeparateCoAuthors(), SplitNameParts()])
    entries = []
    for entry in library.entries:
        fields = entry.fields_dict
        authors = fields["author"].value if "author" in fields else []
        year = re.search(r"\d{4}", str(fields["year"].value)) if "year" in fields else None
        entries.append(
            {
                "key": entry.key,
                "type": entry.entry_type,
                "title": " ".join(str(fields["title"].value).split()) if "title" in fields else "",
                "surnames": [" ".join(name.last) for name in authors if name.last],
                "year": int(year.group(0)) if year else None,
                "doi": str(fields["doi"].value).strip() if "doi" in fields else None,
            }
        )
    return entries


def match_entries(title, truncated, entries):
    """Return the bib entries whose title is (or, if truncated, starts with) title."""
    wanted = fold(title)
    exact = [e for e in entries if (fold(e["title"]).startswith(wanted) if truncated else fold(e["title"]) == wanted)]
    if exact:
        return exact
    # LaTeX in the raw title that editorialbot truncated can defeat the prefix match
    scored = [(difflib.SequenceMatcher(None, wanted, fold(e["title"])[: len(wanted)]).ratio(), e) for e in entries]
    return [e for score, e in sorted(scored, key=lambda s: -s[0])[:1] if score >= 0.85]


def year_of(date_parts):
    try:
        return int(date_parts["date-parts"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def get_json(url, headers=None):
    status, _, body = request(url, "GET", headers)
    if status >= 400:
        raise RuntimeError(f"HTTP {status} from {url}")
    return json.loads(body)


def crossref(entry):
    # Crossref's free-text query.bibliographic ranking can miss a well-known
    # paper entirely (e.g. SciPy 1.0 in Nature Methods), while fielded title and
    # author queries find it, so run both when the entry has authors
    queries = [{"query.bibliographic": " ".join(filter(None, [entry["title"], str(entry["year"] or "")]))}]
    if entry["surnames"]:
        queries.insert(0, {"query.title": entry["title"], "query.author": entry["surnames"][0]})
    items = []
    for query in queries:
        params = {**query, "rows": ROWS, "select": "DOI,title,author,issued,type,container-title"}
        items += get_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params))["message"]["items"]
    return [
        {
            "doi": item["DOI"],
            "source": "Crossref",
            "type": item.get("type"),
            "title": (item.get("title") or [""])[0],
            "year": year_of(item.get("issued")),
            "surnames": [a["family"] for a in item.get("author", []) if "family" in a],
            "container": (item.get("container-title") or [""])[0],
        }
        for item in items
    ]


def datacite(entry):
    tokens = words(entry["title"])
    if not tokens:
        return []
    # Only word characters are passed, so nothing needs escaping in the Lucene query
    params = {"query": f"titles.title:({' '.join(tokens)})", "page[size]": ROWS}
    records = get_json("https://api.datacite.org/dois?" + urllib.parse.urlencode(params))["data"]
    candidates = []
    for record in records:
        attributes = record["attributes"]
        publisher = attributes.get("publisher")
        candidates.append(
            {
                "doi": record["id"],
                "source": "DataCite",
                "type": (attributes.get("types") or {}).get("resourceTypeGeneral"),
                "title": (attributes.get("titles") or [{}])[0].get("title", ""),
                "year": int(attributes["publicationYear"]) if attributes.get("publicationYear") else None,
                # Organizations and unsplit names only have "name", usually as "Family, Given"
                "surnames": [c.get("familyName") or c.get("name", "").split(",")[0] for c in attributes.get("creators", [])],
                "container": publisher.get("name") if isinstance(publisher, dict) else publisher,
            }
        )
    return candidates


def doi_metadata(doi):
    """Return the metadata registered for doi, as a candidate."""
    data = get_json(f"https://doi.org/{urllib.parse.quote(doi, safe='/')}", {"Accept": "application/vnd.citationstyles.csl+json"})
    title = data.get("title")
    return {
        "doi": doi,
        "source": "editorialbot suggestion",
        "type": data.get("type"),
        "title": title[0] if isinstance(title, list) else title or "",
        "year": year_of(data.get("issued")),
        "surnames": [a.get("family") or a.get("literal", "") for a in data.get("author", [])],
        "container": data.get("container-title") or data.get("publisher"),
    }


def score(entry, candidate):
    title = difflib.SequenceMatcher(None, " ".join(words(entry["title"])), " ".join(words(candidate["title"]))).ratio()
    # None means there is nothing to compare, which is neither a match nor a mismatch
    authors = None
    if entry["surnames"] and candidate["surnames"]:
        ours = {w for name in entry["surnames"] for w in words(name)}
        theirs = {w for name in candidate["surnames"] for w in words(name)}
        authors = bool(ours & theirs)
    year = None if entry["year"] is None or candidate["year"] is None else abs(entry["year"] - candidate["year"]) <= 1
    if title >= LIKELY and authors is not False and year is not False:
        verdict = "LIKELY"
    elif title >= POSSIBLE:
        verdict = "POSSIBLE"
    else:
        verdict = None
    return verdict, title, authors, year


def describe(value):
    return {True: "match", False: "MISMATCH", None: "n/a"}[value]


def search(entry, suggested_doi):
    lookups = [("Crossref", lambda: crossref(entry)), ("DataCite", lambda: datacite(entry))]
    if suggested_doi:
        lookups.insert(0, ("doi.org", lambda: [doi_metadata(suggested_doi)]))
    candidates, seen = [], set()
    for name, lookup in lookups:
        try:
            found = lookup()
        # Report a failed registry and keep the others' results
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError, ValueError, KeyError) as e:
            print(f"  REVIEW: {name} lookup failed: {e!r}")
            continue
        for candidate in found:
            if candidate["doi"].casefold() not in seen:
                seen.add(candidate["doi"].casefold())
                candidates.append((score(entry, candidate), candidate))
    shown = [(s, c) for s, c in candidates if s[0]]
    shown.sort(key=lambda sc: (sc[0][0] != "LIKELY", -sc[0][1]))
    return shown[:SHOWN]


def main():
    try:
        check = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        entries = load_bib(sys.argv[2])
    except (OSError, ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("RESULT: ERROR")
        sys.exit(2)

    print(f"reference check: {check['comment_url']} ({check['created_at']})")
    found = 0
    for section in ("skip", "missing"):
        for item in check[section]:
            label = section.upper()
            if item["kind"] == "other":
                print(f"\n{label}: {item['message']}")
                if item["message"].startswith("Entry without DOI or title"):
                    untitled = [e["key"] for e in entries if not e["doi"] and not e["title"]]
                    print(f"  entries without DOI or title: {', '.join(untitled) or 'none (fixed since the check?)'}")
                continue

            suggestion = item.get("doi")
            print(
                f"\n{label}: {item['title']!r}{'...' if item['truncated'] else ''}"
                + (f" (editorialbot suggests {suggestion})" if suggestion else "")
            )
            matches = match_entries(item["title"], item["truncated"], entries)
            if not matches:
                print("  REVIEW: no entry in the bibliography has this title (changed since the check?)")
                continue
            for entry in matches:
                authors = ", ".join(entry["surnames"][:3]) + (" et al." if len(entry["surnames"]) > 3 else "")
                print(
                    f"  entry @{entry['type']}{{{entry['key']}}}: {entry['title']!r}, {authors or 'no authors'}, {entry['year'] or 'no year'}"
                )
                if entry["doi"]:
                    print(f"  REVIEW: the entry now has doi {entry['doi']!r}; rerun @editorialbot check references")
                    continue
                candidates = search(entry, suggestion)
                if not candidates:
                    print("  no candidate DOI found")
                for (verdict, title, author, year), c in candidates:
                    print(
                        f"  {verdict}: {c['doi']} [{c['source']}, {c['type']}] {c['title']!r}, {', '.join(c['surnames'][:3])}, "
                        f"{c['year']}, {c['container'] or 'no container'} (title {title:.2f}, authors {describe(author)}, year {describe(year)})"
                    )
                found += any(verdict == "LIKELY" for (verdict, *_), _ in candidates)

    print(f"\n{found} references with a LIKELY DOI")
    print("RESULT: FOUND" if found else "RESULT: NONE")


if __name__ == "__main__":
    main()
