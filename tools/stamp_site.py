"""Stamp the published site with the commit it was built from, so a page left open (a phone, for hours) can tell a new
version is out and reload onto it (dashboard/version.js). Standard library only. The workflow's site build runs it on
_site once dashboard/ is copied in (.github/workflows/paper.yml), and nothing it does can stop the deploy.

    python3 -m tools.stamp_site <site-dir> <commit>

Each <site-dir>/*.html gets <meta name="desk-build" content="<commit>"> at the start of its head (after the charset:
every script on the page finds it, wherever it is loaded), and every one of the site's own .js and .css files it loads
(a relative src or href on a script or link tag; never Google Fonts or any other absolute URL) a ?v=<short commit>, so
a browser that kept app.js from an earlier deploy (GitHub Pages sends max-age=600) fetches the new one. Then
version.json, {"build": "<commit>"}, last: when it's there, every page carries that build.
The same commit stamps the same bytes (every run on one commit: a run that only brings new boards changes nothing a
page compares), and stamping again changes nothing; another commit's stamp is replaced. Every page is stamped in memory
first, then each file is replaced whole (written beside it, renamed over it), so a failure leaves each file either as
it was or fully stamped; it exits 1 saying why. The workflow then takes out version.json and any <name>.tmp left
beside a page (a run stopped part way, which never got to remove it) and deploys the site as it is.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

COMMIT = re.compile(r"[0-9a-f]{7,64}")
# a script's src or a stylesheet's href (either quote): the files a page loads
ASSET = re.compile(r"""(<(?:script|link)\b[^>]*?\s(?:src|href)=)(["'])([^"'>]*)\2""", re.I)
ABSOLUTE = re.compile(r"[a-z][a-z0-9+.-]*:|[/#?]", re.I)     # https:, data:, //host, /path, #, ?: not one of the site's files
META = re.compile(r'<meta name="desk-build" content="[^"]*">\n?')
HEAD = re.compile(r"<head\b[^>]*>\s*(?:<meta\s+charset=[^>]*>\n?)?", re.I)   # the head's start tag, and the charset after it


def versioned(url: str, short: str) -> str:
    """url with ?v=<short> when it's one of the site's own .js or .css files (a v= it had replaced), else as it is."""
    if ABSOLUTE.match(url):
        return url
    rest, hash_, frag = url.partition("#")
    path, _, query = rest.partition("?")
    if not path.lower().endswith((".js", ".css")):
        return url
    keep = [q for q in query.split("&") if q and not q.startswith("v=")]
    return f"{path}?{'&'.join(keep + [f'v={short}'])}{hash_}{frag}"


def stamp_page(html: str, commit: str) -> str:
    """One page, stamped: its own assets versioned and the build in its head, any earlier stamp replaced."""
    html = ASSET.sub(lambda m: m[1] + m[2] + versioned(m[3], commit[:7]) + m[2], html)
    html = META.sub("", html)
    head = HEAD.search(html)
    if not head:
        raise ValueError("a page has no <head> to put the build in")
    return f'{html[:head.end()]}<meta name="desk-build" content="{commit}">\n{html[head.end():]}'


def _replace(path: Path, data: bytes) -> None:
    """data in path, whole or not at all: written beside it, then renamed over it."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def stamp_site(site: Path, commit: str) -> str:
    commit = commit.strip().lower()
    if not COMMIT.fullmatch(commit):
        raise ValueError("the commit isn't 7 to 64 hex digits")
    pages = sorted(site.glob("*.html"))
    if not pages:
        raise ValueError(f"no pages in {site}")
    old = {p: p.read_bytes() for p in pages}
    new = {p: stamp_page(b.decode("utf-8"), commit).encode("utf-8") for p, b in old.items()}
    for p in pages:
        if new[p] != old[p]:
            _replace(p, new[p])
    _replace(site / "version.json", (json.dumps({"build": commit}) + "\n").encode())
    return f"stamped build {commit[:7]}: {', '.join(p.name for p in pages)}, version.json"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -m tools.stamp_site <site-dir> <commit>", file=sys.stderr)
        return 2
    try:
        print(stamp_site(Path(argv[0]), argv[1]))
    except (OSError, ValueError) as e:          # (a page that isn't UTF-8 is a ValueError too)
        print(f"the site isn't stamped: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
