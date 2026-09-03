#!/usr/bin/env python3
"""Read the phpBB mirror under `oldboard/pages/` into a staging table.

The board that became vBulletin in 2004 ran phpBB before that, twice: 1.4.0 as
«AzzurraNet IRC Network Forum» (2001-2002) and 2.0.x as «Azzurra IRC Network
Forum» (2002-2004). The Wayback mirror of both is `oldboard/pages/`, one file
per topic page, and this reads it into `old_posts` — a staging table, not the
archive proper: merging it into `posts` is a separate step, because `forum.db`
already carries 4928 posts from 2001 and 2002 that vBulletin imported when the
board migrated. Appending would duplicate all of them.

Two parsers, because the two generations share nothing:

* **1.4.0** has no CSS classes at all — `<FONT FACE="Verdana">` and tables. A
  post is a `<TR BGCOLOR>` row holding `Registrato:` in the author cell and
  `Inviato: YYYY-MM-DD HH:MM` in the body cell, with the body between the two
  `<HR>` that fence it. The post id is in the `editpost.php?post_id=` link.
* **2.0.x** (2.0.1, 2.0.2, 2.0.5, 2.0.8 in this mirror, one subSilver template
  between them) names each post with `<a name="1234">` and wraps the body in
  `<span class="postbody">`.

The same topic page was snapshotted many times, so the same post arrives over
and over. Dedup is by phpBB post id and keeps the longest body: a truncated
snapshot is exactly what the extra copies are there to repair.

    python3 oldboard_import.py --db forum.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from forum_import import clean_inline, clean_text, read_page

SCHEMA = """
CREATE TABLE IF NOT EXISTS old_posts (
  post_id    INTEGER PRIMARY KEY,        -- phpBB post id, both generations
  topic_id   INTEGER NOT NULL,
  forum_id   INTEGER,
  seq        INTEGER,                    -- position within the page, 1-based
  page       INTEGER NOT NULL DEFAULT 1, -- `start=` / 15 + 1
  username   TEXT,
  user_id    INTEGER,                    -- phpBB user id, not the vB one
  posted_at  TEXT,                       -- ISO 8601, minute resolution
  subject    TEXT,
  body_html  TEXT NOT NULL,
  body_text  TEXT NOT NULL,
  source     TEXT NOT NULL,              -- 'phpbb14' | 'phpbb20'
  snapshot   TEXT,                       -- Wayback timestamp of the copy kept
  file       TEXT                        -- page it was read from
);
CREATE TABLE IF NOT EXISTS old_topics (
  topic_id   INTEGER PRIMARY KEY,
  forum_id   INTEGER,
  title      TEXT,
  source     TEXT
);
CREATE TABLE IF NOT EXISTS old_forums (
  forum_id   INTEGER PRIMARY KEY,
  name       TEXT
);
CREATE INDEX IF NOT EXISTS old_posts_topic ON old_posts(topic_id, post_id);
CREATE INDEX IF NOT EXISTS old_posts_user  ON old_posts(username);
CREATE INDEX IF NOT EXISTS old_posts_date  ON old_posts(posted_at);
"""

# --- 1.4.0 ------------------------------------------------------------------
# The row that holds one post. Nothing names it — no class, no id — so the
# anchor is its shape: a left-aligned row with a background colour, which is
# what the two alternating post rows have and the rest of the page does not.
# The colours themselves are NOT the anchor: the board was reskinned at least
# once (#EEEEEE/#aeddff early, #F3F3F3/#A8CBFF later) and pinning them to the
# pair seen first silently dropped 655 posts across 125 pages.
RE_14_ROW = re.compile(
    r'<TR[^>]*BGCOLOR="#[0-9A-Fa-f]{6}"[^>]*ALIGN="LEFT"[^>]*>', re.I)
RE_14_USER = re.compile(r'<b>(?P<user>.*?)</b>\s*</FONT>', re.I | re.S)
RE_14_DATE = re.compile(r'Inviato:\s*(?P<date>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})')
RE_14_PID = re.compile(r'(?:editpost\.php\?post_id|reply\.php\?[^"]*?&post)='
                       r'(?P<pid>\d+)', re.I)
RE_14_UID = re.compile(r'bb_profile\.php\?mode=view&user=(?P<uid>\d+)', re.I)
RE_14_CRUMB = re.compile(r'viewforum\.php\?forum=(?P<fid>\d+)[^"]*"[^>]*>'
                         r'(?P<name>[^<]+)</a>', re.I)
RE_14_TOPIC = re.compile(r'<b>»\s*»</b>\s*(?P<title>[^<\n]+?)\s*</TD>',
                         re.I | re.S)

# --- 2.0.x ------------------------------------------------------------------
RE_20_POST = re.compile(
    r'<a name="(?P<pid>\d+)"></a>\s*'
    r'(?:<b><a href="profile\.php\?mode=viewprofile&amp;u=(?P<uid>\d+)[^"]*"'
    r'[^>]*>(?P<user>.*?)</a></b>|<b>(?P<guest>.*?)</b>)',
    re.I | re.S)
RE_20_DATE = re.compile(
    r'Inviato:\s*(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3}),\s*(?P<year>\d{4})'
    r'\s*-\s*(?P<hour>\d{1,2}):(?P<min>\d{2})')
RE_20_SUBJ = re.compile(r'Oggetto del messaggio:\s*(?P<subj>.*?)</span>',
                        re.I | re.S)
RE_20_BODY = re.compile(r'<span class="postbody">', re.I)
RE_20_CRUMB = re.compile(r'viewforum\.php\?f=(?P<fid>\d+)[^"]*"[^>]*>'
                         r'(?P<name>[^<]+)</a>', re.I)
RE_20_TITLE = re.compile(r'<title>[^<]*?Leggi il Topic\s*-\s*(?P<title>[^<]*)'
                         r'</title>', re.I | re.S)

MONTHS = {"gen": "01", "feb": "02", "mar": "03", "apr": "04", "mag": "05",
          "giu": "06", "lug": "07", "ago": "08", "set": "09", "ott": "10",
          "nov": "11", "dic": "12"}

RE_FILE = re.compile(r'topic(?P<topic>\d+)_s(?P<start>\d+)\.html$')


def version(text: str) -> str | None:
    """Which generation wrote this page. 1.4 says so in the page footer; every
    2.0.x in this mirror renders the same subSilver template, so they are one
    parser and the exact patch level does not matter."""
    if "phpBB 1.4.0" in text:
        return "phpbb14"
    if "postbody" in text and "phpBB" in text:
        return "phpbb20"
    return None


def parse_14(text: str, page: int) -> tuple[dict, list[dict]]:
    crumbs = RE_14_CRUMB.findall(text)
    topic = RE_14_TOPIC.search(text)
    head = {"forum_id": int(crumbs[-1][0]) if crumbs else None,
            "forum_name": clean_inline(crumbs[-1][1]) if crumbs else None,
            "title": clean_inline(topic.group("title")) if topic else None}

    starts = [m.start() for m in RE_14_ROW.finditer(text)]
    posts = []
    for i, start in enumerate(starts, start=1):
        end = starts[i] if i < len(starts) else len(text)
        row = text[start:end]
        date = RE_14_DATE.search(row)
        if not date:
            continue           # the header row, or a row with no post in it
        # The body sits between the rule that closes the date line and the one
        # that opens the profile icons. Anchoring on the second `<HR>` rather
        # than on a tag name is what makes this survive the missing classes.
        rules = [m.end() for m in re.finditer(r'<HR>', row, re.I)]
        if len(rules) < 2:
            continue
        body_html = row[rules[0]:rules[1]]
        body_html = re.sub(r'<HR>\s*$', "", body_html, flags=re.I).strip()
        user = RE_14_USER.search(row)
        pid = RE_14_PID.search(row)
        uid = RE_14_UID.search(row)
        posts.append({
            "post_id": int(pid.group("pid")) if pid else None,
            "seq": i,
            "page": page,
            "username": clean_inline(user.group("user")) if user else None,
            "user_id": int(uid.group("uid")) if uid else None,
            "posted_at": date.group("date").replace(" ", "T"),
            "subject": None,
            "body_html": body_html,
            "body_text": clean_text(body_html),
            "source": "phpbb14",
        })
    return head, posts


def parse_20(text: str, page: int) -> tuple[dict, list[dict]]:
    crumbs = RE_20_CRUMB.findall(text)
    title = RE_20_TITLE.search(text)
    head = {"forum_id": int(crumbs[-1][0]) if crumbs else None,
            "forum_name": clean_inline(crumbs[-1][1]) if crumbs else None,
            "title": clean_inline(title.group("title")) if title else None}

    anchors = list(RE_20_POST.finditer(text))
    posts = []
    for i, m in enumerate(anchors, start=1):
        end = anchors[i].start() if i < len(anchors) else len(text)
        block = text[m.end():end]
        date = RE_20_DATE.search(block)
        body = RE_20_BODY.search(block)
        if not (date and body):
            continue
        # The body ends where the «modificato da» line begins: that empty
        # gensmall span is emitted right after it, and unlike `</td>` it cannot
        # appear inside a quote or a code block.
        tail = block.find('<span class="gensmall">', body.end())
        if tail < 0:
            tail = block.find("</td>", body.end())
        body_html = block[body.end():tail if tail > 0 else len(block)]
        body_html = re.sub(r'</span>\s*$', "", body_html.strip()).strip()
        subj = RE_20_SUBJ.search(block[:body.start()])
        posts.append({
            "post_id": int(m.group("pid")),
            "seq": i,
            "page": page,
            "username": clean_inline(m.group("user") or m.group("guest") or ""),
            "user_id": int(m.group("uid")) if m.group("uid") else None,
            "posted_at": (f"{date.group('year')}-"
                          f"{MONTHS.get(date.group('mon').lower(), '01')}-"
                          f"{int(date.group('day')):02d}T"
                          f"{int(date.group('hour')):02d}:{date.group('min')}"),
            "subject": clean_inline(subj.group("subj")) if subj else None,
            "body_html": body_html,
            "body_text": clean_text(body_html),
            "source": "phpbb20",
        })
    return head, posts


def snapshots(root: Path) -> dict[str, str]:
    """file name → the Wayback timestamp it was fetched at, from the crawl list."""
    out: dict[str, str] = {}
    targets = root / "targets.tsv"
    if not targets.exists():
        return out
    for line in targets.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.setdefault(parts[2], parts[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="forum.db")
    ap.add_argument("--root", default="oldboard",
                    help="the crawl directory (pages/ and targets.tsv)")
    args = ap.parse_args()

    root = Path(args.root)
    pages = sorted((root / "pages").glob("*.html"))
    if not pages:
        print(f"no pages under {root}/pages", file=sys.stderr)
        return 1
    stamps = snapshots(root)

    # post id → row, longest body wins. Same post, many snapshots: the copies
    # differ only by how much of the page the Archive managed to save.
    best: dict[int, dict] = {}
    topics: dict[int, dict] = {}
    forums: dict[int, str] = {}
    seen = skipped = anon = 0

    for path in pages:
        m = RE_FILE.search(path.name)
        if not m:
            continue
        topic_id = int(m.group("topic"))
        page = int(m.group("start")) // 15 + 1
        text = read_page(path)
        kind = version(text)
        if kind is None:
            skipped += 1
            continue
        head, posts = (parse_14 if kind == "phpbb14" else parse_20)(text, page)

        if head["forum_id"] and head["forum_name"]:
            forums.setdefault(head["forum_id"], head["forum_name"])
        if head["title"]:
            topics.setdefault(topic_id, {"forum_id": head["forum_id"],
                                         "title": head["title"],
                                         "source": kind})
        for p in posts:
            seen += 1
            if p["post_id"] is None:
                # 1.4.0 hides the edit link on a locked topic; without an id the
                # copy cannot be deduped against the others, so it is counted
                # and dropped rather than let in as a phantom duplicate.
                anon += 1
                continue
            p |= {"topic_id": topic_id, "forum_id": head["forum_id"],
                  "snapshot": stamps.get(path.name), "file": path.name}
            old = best.get(p["post_id"])
            if old is None or len(p["body_text"]) > len(old["body_text"]):
                best[p["post_id"]] = p

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    db.execute("DELETE FROM old_posts")
    db.execute("DELETE FROM old_topics")
    db.execute("DELETE FROM old_forums")
    db.executemany(
        "INSERT INTO old_posts (post_id, topic_id, forum_id, seq, page,"
        " username, user_id, posted_at, subject, body_html, body_text,"
        " source, snapshot, file) VALUES (:post_id, :topic_id, :forum_id,"
        " :seq, :page, :username, :user_id, :posted_at, :subject, :body_html,"
        " :body_text, :source, :snapshot, :file)", best.values())
    db.executemany("INSERT INTO old_topics (topic_id, forum_id, title, source)"
                   " VALUES (?, ?, ?, ?)",
                   [(t, v["forum_id"], v["title"], v["source"])
                    for t, v in topics.items()])
    db.executemany("INSERT INTO old_forums (forum_id, name) VALUES (?, ?)",
                   list(forums.items()))
    db.commit()

    rows = db.execute("SELECT source, count(*), min(posted_at), max(posted_at)"
                      " FROM old_posts GROUP BY source").fetchall()
    print(f"pages={len(pages)} unparsable={skipped} post occurrences={seen} "
          f"senza id={anon} distinti={len(best)}")
    for src, n, lo, hi in rows:
        print(f"  {src}: {n} post, {lo} → {hi}")
    print(f"topics={len(topics)} forums={len(forums)}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
