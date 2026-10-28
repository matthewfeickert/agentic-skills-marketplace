"""Check that the URLs and DOIs in a JOSS paper resolve.

Usage: check_urls.py [--workers N] [--extra URL_OR_DOI ...] <file> [<file> ...]

Collects every http(s) URL from the given files (paper.md, paper.bib) and
every `doi` field from .bib files, then checks each one once. The .bib files
are read with bibtexparser, so fields that span several lines are handled.

DOIs, including https://doi.org/ URLs, are checked against the DOI handle API
(https://www.doi.org/the-identifier/resources/factsheets/doi-resolution-documentation)
instead of by following them to the publisher, because many publishers answer
scripted requests with 403 even when the DOI is valid.

Every result is printed as one of:

- OK: the URL resolved (with the final URL when it redirected elsewhere).
- REVIEW: the server refused or failed the request (401, 403, 429, 5xx,
  timeouts, TLS errors). The link may still work in a browser.
- BROKEN: 404/410, an unknown host, or a DOI that is not registered.

followed by a RESULT line:

- RESULT: OK, exit 0: nothing is BROKEN.
- RESULT: BROKEN, exit 1: at least one URL or DOI is BROKEN.
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import bibtexparser

TIMEOUT = 30  # seconds per request
ATTEMPTS = 3
BACKOFF = 2  # seconds, doubled after each failed attempt: 2, 4
USER_AGENT = "joss-paper-checks (+https://github.com/matthewfeickert/agentic-skills-marketplace)"

# Stops at whitespace and at the delimiters that close a URL in Markdown
# (`)`, `]`, `>`) and LaTeX (`\url{...}`). URLs that themselves contain these
# characters are rare in papers, and are then reported as BROKEN for review.
URL = re.compile(r"https?://[^\s<>\"'(){}\[\]]+")
DOI_URL = re.compile(r"^https?://(?:dx\.)?doi\.org/(.+)$", re.IGNORECASE)
# LaTeX escapes that occur in URLs. The fields are not decoded with
# pylatexenc, as it would read an unescaped % in a URL as a comment.
BIB_ESCAPES = {r"\_": "_", r"\%": "%", r"\&": "&", r"\#": "#", r"\~{}": "~"}
# Status codes servers use to refuse scripted requests rather than to say the page is gone
REFUSED = {401, 403, 429, 999}


def collect(paths, extra):
    """Return {target: [locations]}, where a target is ("url", url) or ("doi", doi)."""
    targets = defaultdict(list)

    def add(value, location):
        value = value.rstrip(".,;:!?")
        doi = DOI_URL.match(value)
        if doi:
            target = ("doi", urllib.parse.unquote(doi.group(1)))
        elif value.startswith(("http://", "https://")):
            target = ("url", value)
        else:
            target = ("doi", re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE))
        # An entry's doi and url fields often hold the same DOI
        if location not in targets[target]:
            targets[target].append(location)

    for path in paths:
        if path.endswith(".bib"):
            for entry in bibtexparser.parse_file(path).entries:
                location = f"{path}:{entry.start_line + 1} ({entry.key})"
                for field in entry.fields:
                    value = str(field.value)
                    for escaped, plain in BIB_ESCAPES.items():
                        value = value.replace(escaped, plain)
                    if field.key.lower() == "doi":
                        add(value.strip(), location)
                    else:
                        for match in URL.finditer(value):
                            add(match.group(0), location)
        else:
            with open(path, encoding="utf-8") as f:
                for number, line in enumerate(f, start=1):
                    for match in URL.finditer(line):
                        add(match.group(0), f"{path}:{number}")
    for value in extra:
        add(value, "issue")
    return targets


def request(url, method, headers=None, read_body=False):
    """Return (status, final_url, body), retrying 429, 5xx, timeouts and connection errors.

    The body is empty unless read_body is set. find_dois.py also uses this.
    """
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                return response.status, response.url, response.read() if read_body else b""
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                return e.code, url, b""
            error = e
        except urllib.error.URLError as e:
            # An unknown host will not appear on retry
            if isinstance(e.reason, socket.gaierror):
                raise
            error = e
        except (TimeoutError, ConnectionError) as e:
            error = e
        if attempt == ATTEMPTS - 1:
            raise error
        time.sleep(BACKOFF * 2**attempt)


def check_doi(doi):
    api = "https://doi.org/api/handles/" + urllib.parse.quote(doi, safe="/")
    status, _, body = request(api, "GET", read_body=True)
    # The handle API answers 404 with responseCode 100 for an unregistered DOI
    if status == 404:
        return "BROKEN", "DOI is not registered"
    if status >= 400:
        return "REVIEW", f"DOI handle API returned HTTP {status}"
    code = json.loads(body).get("responseCode")
    if code == 1:
        return "OK", f"https://doi.org/{doi}"
    return "BROKEN", f"DOI handle API responseCode {code}"


def check_url(url):
    status, final, _ = request(url, "HEAD")
    # Some servers reject or mishandle HEAD, so confirm any failure with GET
    if status >= 400:
        status, final, _ = request(url, "GET")
    if status < 400:
        return "OK", f"redirects to {final}" if final.rstrip("/") != url.rstrip("/") else f"HTTP {status}"
    if status in {404, 410}:
        return "BROKEN", f"HTTP {status}"
    if status in REFUSED:
        return "REVIEW", f"HTTP {status}, the server may block scripted requests"
    return "REVIEW", f"HTTP {status}"


def check(target):
    kind, value = target
    try:
        return check_doi(value) if kind == "doi" else check_url(value)
    except urllib.error.HTTPError as e:
        return "REVIEW", f"HTTP {e.code} after {ATTEMPTS} attempts"
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.gaierror):
            return "BROKEN", f"unknown host ({e.reason})"
        return "REVIEW", f"request failed after {ATTEMPTS} attempts: {e.reason}"
    except (TimeoutError, ConnectionError) as e:
        return "REVIEW", f"request failed after {ATTEMPTS} attempts: {e!r}"
    except json.JSONDecodeError:
        return "REVIEW", "DOI handle API returned invalid JSON"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--extra", nargs="*", default=[], help="additional URLs or DOIs, e.g. from the review issue")
    # The checks are network bound, but stay within 80% of the machine's CPUs
    default_workers = max(1, min(8, int(0.8 * (os.cpu_count() or 1))))
    parser.add_argument("--workers", type=int, default=default_workers)
    args = parser.parse_args()

    targets = collect(args.files, args.extra)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = dict(zip(targets, pool.map(check, targets)))

    order = {"BROKEN": 0, "REVIEW": 1, "OK": 2}
    for (kind, value), (status, detail) in sorted(results.items(), key=lambda item: (order[item[1][0]], item[0])):
        label = f"doi:{value}" if kind == "doi" else value
        print(f"{status}: {label} ({detail}) at {', '.join(targets[(kind, value)])}")

    counts = Counter(status for status, _ in results.values())
    print(f"checked {len(results)}: {counts['OK']} OK, {counts['REVIEW']} REVIEW, {counts['BROKEN']} BROKEN")
    if counts["BROKEN"]:
        print("RESULT: BROKEN")
        sys.exit(1)
    print("RESULT: OK")


if __name__ == "__main__":
    main()
