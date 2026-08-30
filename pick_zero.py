#!/usr/bin/env python3
"""Promote the best retry/ candidate into pages/.

retry_zero.sh downloads every snapshot it can find for the threads that imported
with zero posts, one file per snapshot (`<name>.html.<timestamp>`). File size is a
liar here — a head-only truncation can be bigger than a short but complete thread —
so each candidate is actually parsed with the importer's own parsers and scored by
how many posts come out, with a complete body beating a truncated one on a tie.

A candidate is promoted into pages/ only if it scores strictly better than what is
already there. Nothing is deleted: the loser stays in retry/.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import forum_import as fi

HERE = Path(__file__).resolve().parent
PAGES = HERE / "pages"
RETRY = HERE / "retry"


def score(path: Path) -> tuple[int, int]:
    """(posts parsed, posts with a whole body) — bigger is better, ties broken by
    how many of those bodies the snapshot did not cut off."""
    if not path.exists() or path.stat().st_size == 0:
        return (0, 0)
    text = fi.read_page(path)
    name = path.name.split(".html")[0] + ".html"
    for regex, parser in ((fi.RE_LOFI, fi.parse_lofi), (fi.RE_FULL, fi.parse_full)):
        m = regex.match(name)
        if m:
            _title, _forum, posts = parser(text, fi.page_no(m.group(2)))
            return (len(posts), sum(1 for p in posts if not p["truncated"]))
    return (0, 0)


def main() -> int:
    dry = "--dry-run" in sys.argv
    best: dict[str, tuple[tuple[int, int], Path]] = {}
    for cand in sorted(RETRY.glob("*.html.*")):
        target = cand.name.split(".html")[0] + ".html"
        s = score(cand)
        if target not in best or s > best[target][0]:
            best[target] = (s, cand)

    promoted = unchanged = 0
    for target, (s, cand) in sorted(best.items()):
        current = score(PAGES / target)
        if s > current:
            print(f"{target}: {current} -> {s}  ({cand.name})")
            if not dry:
                shutil.copy2(cand, PAGES / target)
            promoted += 1
        else:
            unchanged += 1

    print(f"{promoted} promoted, {unchanged} left alone, {len(best)} targets"
          + (" (dry run)" if dry else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
