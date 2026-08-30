#!/usr/bin/env python3
"""Build a thread catalogue from the downloaded vBulletin lo-fi pages.

Reads pages/f-*.html (forum indexes) and pages/t-*.html (threads) and writes
index.json: one record per thread with title, forum, post count and authors.
"""
import html
import json
import re
from pathlib import Path

PAGES = Path(__file__).parent / "pages"
OUT = Path(__file__).parent / "index.json"

THREAD_LINK = re.compile(r'href="[^"]*?(t-(\d+)(?:-p-\d+)?\.html)">(.*?)</a>', re.S)
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
BREADCRUMB = re.compile(r'<div class="navbar">(.*?)</div>', re.S)
POST = re.compile(r'<div class="post">(.*?)</div>', re.S)
POSTER = re.compile(r"<strong>(.*?)</strong>\s*(\d{2}-\d{2}-\d{4}[^<]*)", re.S)
TAG = re.compile(r"<[^>]+>")


def text(s):
    return html.unescape(TAG.sub("", s)).strip()


def read(p):
    return p.read_text(encoding="iso-8859-1", errors="replace")


def main():
    forums = {}
    threads = {}

    for f in sorted(PAGES.glob("f-*.html")):
        d = read(f)
        fid = f.stem.split("-")[1]
        t = TITLE.search(d)
        forums[fid] = text(t.group(1)) if t else ""
        for _, tid, name in THREAD_LINK.findall(d):
            threads.setdefault(tid, {"id": int(tid), "forum": fid})
            threads[tid].setdefault("title", text(name))

    for f in sorted(PAGES.glob("t-*.html")):
        m = re.match(r"t-(\d+)", f.stem)
        if not m:
            continue
        tid = m.group(1)
        d = read(f)
        rec = threads.setdefault(tid, {"id": int(tid)})
        t = TITLE.search(d)
        if t:
            rec["title"] = text(t.group(1)).replace(
                "Azzurra IRC Network Forum - ", "")
        posters = [(text(a), b.strip()) for a, b in POSTER.findall(d)]
        rec["posts"] = rec.get("posts", 0) + len(posters)
        rec.setdefault("authors", [])
        for a, _ in posters:
            if a and a not in rec["authors"]:
                rec["authors"].append(a)
        if posters:
            rec.setdefault("first_post_date", posters[0][1])
        rec.setdefault("files", []).append(f.name)

    out = sorted(threads.values(), key=lambda r: r["id"])
    OUT.write_text(json.dumps(
        {"forums": forums, "threads": out}, ensure_ascii=False, indent=1))
    downloaded = sum(1 for r in out if r.get("files"))
    print(f"forums={len(forums)} threads_known={len(out)} "
          f"threads_downloaded={downloaded} "
          f"posts={sum(r.get('posts', 0) for r in out)}")


if __name__ == "__main__":
    main()
