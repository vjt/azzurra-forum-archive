#!/usr/bin/env python3
"""assets_list.py — every image the posts point at, one line each.

Two families, both dead since the board went down: the smilies the board
served itself (12627 references, a couple of hundred files) and whatever the
posters hotlinked from elsewhere (903 URLs on ~200 hosts).  The Archive has
the first family — measured, `icon_smile.gif` comes back 200 image/gif — and
some of the second.

Output: `url<TAB>outname`, deduped.  The name is the sha1 of the URL plus the
original extension: the URLs collide on basename constantly (twenty different
`image.jpg`) and the hash is the only thing that does not.
"""
import hashlib
import re
import sqlite3
import sys

RE_IMG = re.compile(r'<img[^>]*src="([^"]+)"', re.I)
RE_BB = re.compile(r"\[img\]\s*([^\[\s]{1,300}?)\s*\[/img\]", re.I)
RE_SMILEY_SRC = re.compile(r"images/smil(?:ies|es)/", re.I)
BOARD = "http://forum.azzurra.org/"


def outname(url: str) -> str:
    ext = re.sub(r"[^a-z0-9]", "", url.split("?")[0].rsplit(".", 1)[-1].lower())[:4]
    return hashlib.sha1(url.encode()).hexdigest() + "." + (ext or "bin")


def main() -> None:
    db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "forum.db")
    seen: dict[str, str] = {}
    for (body,) in db.execute("SELECT body_html FROM posts"):
        if not body:
            continue
        for url in RE_IMG.findall(body) + RE_BB.findall(body):
            url = url.strip().replace("&amp;", "&")
            if not url or url.startswith(("data:", "javascript:")):
                continue
            # The board's own relative paths: `images/smilies/classic/x.gif`.
            if not url.lower().startswith(("http://", "https://")):
                if not RE_SMILEY_SRC.search(url):
                    continue
                url = BOARD + url.lstrip("/")
            seen.setdefault(url, outname(url))
    for url, name in sorted(seen.items()):
        print(f"{url}\t{name}")
    print(f"{len(seen)} urls", file=sys.stderr)


if __name__ == "__main__":
    main()
