#!/usr/bin/env python3
"""
forum_import.py — turn the scraped forum.azzurra.org HTML into a queryable SQLite DB.

Two page shapes come out of the Wayback scrape and BOTH are parsed here:

  * lofi archive  — `t-<id>.html`, `t-<id>-p-<page>.html`, indexes `f-<id>*.html`.
    Machine-generated, tiny, one `div.post` per message. No post ids, no member ids.

  * full vBulletin — `st-<id>.html`, `st-<id>-p-<page>.html` (from `showthread.php`).
    Carries the real vB post id, the member id and an absolute post number, so it wins
    whenever the same (thread, seq) exists in both.

Everything is ISO-8859-1 on disk (see the scrape notes): decoded to UTF-8 on read, once,
here. Nothing downstream should ever have to think about the encoding again.

The DB is rebuilt from scratch on every run — the whole point of keeping the HTML is that
the import is cheap and repeatable, so a parser fix costs one command, not a migration.

Usage:  ./forum_import.py [--pages DIR] [--db FILE] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

# ── page-shape recognition ────────────────────────────────────────────────
# `t-1739` (no extension) exists too — one file the fetcher wrote before the
# suffix was appended, so the extension is optional in every pattern.
RE_FORUM = re.compile(r"^f-(\d+)(?:-p-(\d+))?(?:\.html)?$")
RE_LOFI = re.compile(r"^t-(\d+)(?:-p-(\d+))?(?:\.html)?$")
RE_FULL = re.compile(r"^st-(\d+)(?:-p-(\d+))?(?:\.html)?$")

# ── lofi archive markup ───────────────────────────────────────────────────
# Many Archive snapshots are cut mid-body: the head is there, the closing `</div>` of
# the last `posttext` never arrives. Requiring it threw the whole page away and left the
# thread at zero posts. So the body may end at EOF instead — the tail is truncated, not
# absent, and half a post from 2001 beats none.
RE_LOFI_POST = re.compile(
    r'<div class="post">.*?'
    r'<div class="username">(?P<user>.*?)</div>.*?'
    r'<div class="date">(?P<date>.*?)</div>.*?'
    r'<div class="posttext">',
    re.S,
)
BOARD = r"Azzurra IRC Network Forum"
# Three `<title>` shapes across ten years of snapshots: the lofi archive one, the old
# lofi one with the board name FIRST, and the full showthread one.
RE_TITLES = (
    re.compile(r"<title>\s*(?P<title>.*?)\s*\[Archivio\]", re.S),
    re.compile(rf"<title>\s*{BOARD}\s*-\s*(?P<title>.*?)\s*</title>", re.S),
    re.compile(rf"<title>\s*(?P<title>.*?)\s*-\s*{BOARD}\s*</title>", re.S),
)
# Paginated showthread titles carry the page number; the thread's name does not.
RE_TITLE_PAGE = re.compile(r"\s*-\s*Pagina\s+\d+\s*$", re.I)
RE_LOFI_CRUMB = re.compile(r'href="[^"]*/archive/index\.php/f-(\d+)\.html"[^>]*>(?P<name>[^<]*)</a>')

# ── full vBulletin markup ─────────────────────────────────────────────────
# The date is NOT delimited by `<!-- status icon and date -->` in every skin: the
# 2001-2003 template (`azzurra2.0`, low thread ids) drops those comments and leaves
# the date bare inside `td.thead`. Requiring them silently zeroed 1939 threads whose
# pages were on disk and perfectly readable. So capture the whole head of the post
# table instead and let `parse_date`/`RE_DATE` find the stamp wherever it sits — the
# region is scoped between `<table id="postN">` and `id="postcountN"` in both skins,
# which is exactly where the date lives and nothing else does.
RE_FULL_POST = re.compile(
    r'<table id="post(?P<pid>\d+)"(?P<date>.*?)'
    r'id="postcount(?P=pid)"[^>]*name="(?P<seq>\d+)".*?'
    r'(?:<a class="bigusername" href="(?P<href>[^"]*)"[^>]*>(?P<user>.*?)</a>'
    r'|<div id="postmenu_(?P=pid)">\s*(?P<user2>[^<]*?)\s*</div>).*?'
    r'<div id="post_message_(?P=pid)">',
    re.S,
)

# The body ends at the `</div>` that MATCHES its opening tag, not at the first one:
# vB3 renders quote and code boxes as `<div style="margin:20px"><div class="smallfont">
# Cita:</div><table>…`, so a non-greedy `.*?</div>` cut 2744 posts at the word "Cita:"
# and 36 at "Codice:" — and set `truncated` to 0 on all of them, because the regex was
# perfectly happy. Count the nesting instead. `\Z` still terminates: an Archive snapshot
# cut mid-post has no closing tag at all, and half a post beats none.
RE_DIV_TAG = re.compile(r"<(/?)div\b", re.I)


def div_body(text: str, start: int) -> tuple[str, bool]:
    """Slice `text` from `start` (just past an opening `<div>`) to its matching close.
    Returns (body, truncated) — truncated when the snapshot ends before the tag does."""
    depth = 1
    for m in RE_DIV_TAG.finditer(text, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return text[start:m.start()], False
    return text[start:], True
RE_MEMBER_ID = re.compile(r"[?&](?:amp;)?u=(\d+)")
RE_FULL_CRUMB = re.compile(r'href="[^"]*forumdisplay\.php\?[^"]*?f=(\d+)"[^>]*>\s*(?P<name>[^<]*?)\s*</a>')

# ── forum index markup ────────────────────────────────────────────────────
RE_INDEX_NAME = re.compile(r'Mostra versione intera\s*:\s*<a href="[^"]*"\s*>(?P<name>[^<]*)</a>')
RE_INDEX_THREAD = re.compile(r'<li><a href="t-(\d+)\.html">(?P<title>.*?)</a></li>', re.S)

# vBulletin renders dd-mm-yyyy, hh:mm in this board's locale — except on 460
# stamps, where the member had the English 12-hour clock and the page says
# `01:21 PM`. Reading the hour and dropping the marker put those posts twelve
# hours early, which is why 150 threads showed an afternoon reply above the
# morning one it answered. The marker is optional and only present on those.
RE_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4}),?\s*(\d{2}):(\d{2})"
                     r"(?:\s*(?P<ampm>[AP]M))?", re.I)

RE_TAG = re.compile(r"<[^>]+>")
RE_BR = re.compile(r"<br\s*/?>", re.I)
RE_WS = re.compile(r"[ \t]+")
RE_NL = re.compile(r"\n{3,}")

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE forums (
  id          INTEGER PRIMARY KEY,
  name        TEXT,
  thread_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE threads (
  id        INTEGER PRIMARY KEY,
  forum_id  INTEGER REFERENCES forums(id),
  title     TEXT,
  post_count INTEGER NOT NULL DEFAULT 0,
  first_post_at TEXT,
  last_post_at  TEXT
);

CREATE TABLE posts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id  INTEGER NOT NULL REFERENCES threads(id),
  seq        INTEGER NOT NULL,          -- position in the thread, 1-based
  page       INTEGER NOT NULL DEFAULT 1,
  vb_post_id INTEGER,                   -- real vBulletin post id (showthread only)
  username   TEXT,
  member_id  INTEGER,                   -- real vBulletin user id (showthread only)
  posted_at  TEXT,                      -- ISO 8601, minute resolution
  body_html  TEXT NOT NULL,
  body_text  TEXT NOT NULL,
  source     TEXT NOT NULL,             -- 'lofi' | 'showthread'
  truncated  INTEGER NOT NULL DEFAULT 0, -- 1 = the snapshot was cut inside this body
  UNIQUE (thread_id, seq)
);

CREATE INDEX posts_thread ON posts(thread_id, seq);
CREATE INDEX posts_user   ON posts(username);
CREATE INDEX posts_date   ON posts(posted_at);

CREATE VIRTUAL TABLE posts_fts USING fts5(
  username, body_text,
  content='posts', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
"""


# vB3 preloads the thread's other pages into a JS array so "show post" needs no
# reload: `pd[24560] = '<!-' + '- post #24560 -' + '->\r\n…<table id="post24560">…';`
# — one long single-quoted line per post, with the whitespace backslash-escaped and
# the HTML comments split across string concatenations to keep the browser from
# closing the block early.  The post regexes matched inside it anyway and stored 218
# posts whose markup carried literal `\r\n`, which no later pass could read.  Decode
# the block back into plain HTML instead of dropping it: on 90 pages it is the only
# copy of posts whose own page never got snapshotted, and once decoded the duplicates
# collapse under the existing same-author/same-minute/same-body dedup.
RE_JS_CACHE = re.compile(r"^\s*pd\[\d+\]\s*=\s*'(?P<js>.*)';\s*$", re.M)
RE_JS_JOIN = re.compile(r"'\s*\+\s*'")
RE_JS_ESC = re.compile(r"\\(.)")
JS_UNESC = {"r": "\r", "n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"', "/": "/"}


def unpack_js_cache(text: str) -> str:
    def one(m: re.Match[str]) -> str:
        js = RE_JS_JOIN.sub("", m.group("js"))
        return RE_JS_ESC.sub(lambda e: JS_UNESC.get(e.group(1), e.group(1)), js)
    return RE_JS_CACHE.sub(one, text)


def read_page(path: Path) -> str:
    """Decode one scraped page. Everything on disk is ISO-8859-1; a few pages carry
    stray bytes, so decoding never raises — a broken byte is worth less than the post."""
    text = path.read_bytes().decode("iso-8859-1", errors="replace")
    return unpack_js_cache(text) if "pd[" in text else text


def clean_text(fragment: str) -> str:
    """HTML fragment → plain text, keeping paragraph structure."""
    text = RE_BR.sub("\n", fragment)
    text = RE_TAG.sub("", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    text = RE_WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return RE_NL.sub("\n\n", text).strip()


def clean_inline(fragment: str) -> str:
    return clean_text(fragment).replace("\n", " ").strip()


def extract_title(text: str) -> str | None:
    """The thread name, whichever of the three `<title>` shapes this snapshot uses."""
    for regex in RE_TITLES:
        m = regex.search(text)
        if m:
            title = RE_TITLE_PAGE.sub("", clean_inline(m.group("title")))
            if title:
                return title
    return None


def parse_date(raw: str) -> str | None:
    m = RE_DATE.search(raw)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()[:5]
    ampm = (m.group("ampm") or "").upper()
    if ampm:
        h = int(hour) % 12          # 12 AM is midnight, 12 PM is noon
        hour = f"{h + (12 if ampm == 'PM' else 0):02d}"
    return f"{year}-{month}-{day}T{hour}:{minute}"


def page_no(raw: str | None) -> int:
    return int(raw) if raw else 1


def parse_lofi(text: str, page: int) -> tuple[str | None, int | None, list[dict]]:
    """lofi archive page → (thread title, forum id, posts). Post ids do not exist in
    this shape, so `seq` is the position within the page, offset by the caller."""
    title = extract_title(text)

    crumbs = RE_LOFI_CRUMB.findall(text)
    forum_id = int(crumbs[-1][0]) if crumbs else None

    posts = []
    for i, m in enumerate(RE_LOFI_POST.finditer(text), start=1):
        raw_body, cut = div_body(text, m.end())
        body_html = raw_body.strip()
        posts.append(
            {
                "seq": i,
                "page": page,
                "vb_post_id": None,
                "username": clean_inline(m.group("user")),
                "member_id": None,
                "posted_at": parse_date(m.group("date")),
                "body_html": body_html,
                "body_text": clean_text(body_html),
                "source": "lofi",
                "truncated": int(cut),
            }
        )
    return title, forum_id, posts


def parse_full(text: str, page: int) -> tuple[str | None, int | None, list[dict]]:
    """full vBulletin showthread page → (thread title, forum id, posts). This shape
    carries the absolute post number, so `seq` is authoritative and page-independent."""
    title = extract_title(text)

    crumbs = RE_FULL_CRUMB.findall(text)
    forum_id = int(crumbs[-1][0]) if crumbs else None

    posts = []
    for m in RE_FULL_POST.finditer(text):
        raw_body, cut = div_body(text, m.end())
        body_html = raw_body.strip()
        user = m.group("user") or m.group("user2") or ""
        uid_m = RE_MEMBER_ID.search(m.group("href") or "")
        posts.append(
            {
                "seq": int(m.group("seq")),
                "page": page,
                "vb_post_id": int(m.group("pid")),
                "username": clean_inline(user),
                "member_id": int(uid_m.group(1)) if uid_m else None,
                "posted_at": parse_date(m.group("date")),
                "body_html": body_html,
                "body_text": clean_text(body_html),
                "source": "showthread",
                "truncated": int(cut),
            }
        )
    return title, forum_id, posts


def load_forum_indexes(files: list[Path], db: sqlite3.Connection) -> dict[int, int]:
    """Read `f-*` indexes for forum names and the thread → forum mapping. The mapping
    is a fallback: a thread page carries its own breadcrumb and that wins."""
    names: dict[int, str] = {}
    thread_forum: dict[int, int] = {}
    for path in files:
        m = RE_FORUM.match(path.name)
        if not m:
            continue
        fid = int(m.group(1))
        text = read_page(path)
        name_m = RE_INDEX_NAME.search(text)
        if name_m and fid not in names:
            names[fid] = clean_inline(name_m.group("name"))
        for tid, _title in RE_INDEX_THREAD.findall(text):
            thread_forum[int(tid)] = fid
    db.executemany(
        "INSERT OR REPLACE INTO forums (id, name) VALUES (?, ?)",
        sorted(names.items()),
    )
    return thread_forum


def reorder_by_post_id(db: sqlite3.Connection) -> int:
    """Put each page back in the order vBulletin itself gave the posts.

    `seq` is the position the post held *in the snapshot*, and two snapshots of
    the same page taken months apart do not agree: a post deleted in between
    shifts everything after it, so the merge of the two interleaves them and a
    reply lands above the line it answers. The real post id says what the order
    was, and it is monotonic in time across the whole corpus.

    Only posts that carry an id are touched, and only where the whole group has
    one: a thread half read from the lo-fi view has nothing to sort by. A thread
    where every post has an id is sorted whole — the page size was not constant
    across the crawls either, so a thread can hold a page 1 of twenty posts and
    a page 2 of ten that overlap it — and otherwise page by page, which at least
    repairs the snapshots that disagree inside one page.
    """
    groups = [
        (r[0], None)
        for r in db.execute(
            "SELECT thread_id FROM posts GROUP BY thread_id "
            " HAVING count(*) = count(vb_post_id) AND count(*) > 1"
        )
    ]
    whole = {t for t, _ in groups}
    groups += [
        (r[0], r[1])
        for r in db.execute(
            "SELECT thread_id, page FROM posts GROUP BY thread_id, page "
            " HAVING count(*) = count(vb_post_id) AND count(*) > 1"
        )
        if r[0] not in whole
    ]
    # A thread read only from the lo-fi view has no post ids at all, and there
    # the clock is the only order there is. Sort those by the stamp, keeping the
    # snapshot's order for posts of the same minute — it repairs the handful of
    # threads whose pages were crawled under two different page sizes and so
    # overlap at the boundary, and leaves every thread already in order alone.
    groups += [
        (r[0], "clock")
        for r in db.execute(
            "SELECT thread_id FROM posts GROUP BY thread_id "
            " HAVING count(vb_post_id) = 0 AND count(*) > 1"
        )
    ]
    moved = 0
    for thread_id, page in groups:
        if page == "clock":
            rows = db.execute(
                "SELECT id, seq, posted_at FROM posts WHERE thread_id = ? ORDER BY seq",
                (thread_id,),
            ).fetchall()
        elif page is None:
            rows = db.execute(
                "SELECT id, seq, vb_post_id FROM posts WHERE thread_id = ? ORDER BY seq",
                (thread_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, seq, vb_post_id FROM posts WHERE thread_id = ? AND page = ? ORDER BY seq",
                (thread_id, page),
            ).fetchall()
        # A post whose stamp the snapshot lost sorts last rather than raising.
        want = sorted(rows, key=lambda r: (r[2] is None, r[2]))
        if [r[0] for r in want] == [r[0] for r in rows]:
            continue
        slots = sorted(r[1] for r in rows)
        # UNIQUE(thread_id, seq) forbids the permutation in place: park the rows
        # at -seq first, exactly as the phpBB merge does when it renumbers.
        db.executemany("UPDATE posts SET seq = -seq WHERE id = ?", [(r[0],) for r in rows])
        db.executemany(
            "UPDATE posts SET seq = ? WHERE id = ?",
            [(slot, r[0]) for slot, r in zip(slots, want)],
        )
        moved += sum(1 for a, b in zip(rows, want) if a[0] != b[0])
    return moved


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=Path, default=here / "pages", help="directory of scraped HTML")
    ap.add_argument("--db", type=Path, default=here / "forum.db", help="SQLite file to (re)build")
    ap.add_argument("--limit", type=int, default=0, help="stop after N thread pages (debug)")
    args = ap.parse_args()

    if not args.pages.is_dir():
        print(f"forum_import: no such directory: {args.pages}", file=sys.stderr)
        return 2

    if args.db.exists():
        args.db.unlink()
    for suffix in ("-wal", "-shm"):
        stale = args.db.with_name(args.db.name + suffix)
        if stale.exists():
            stale.unlink()

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)

    files = sorted(args.pages.iterdir())
    thread_forum = load_forum_indexes(files, db)
    print(f"forums: {len(thread_forum)} threads mapped from indexes", file=sys.stderr)

    # (thread, seq) → post. showthread wins over lofi: it carries the real ids.
    collected: dict[tuple[int, int], dict] = {}
    titles: dict[int, str] = {}
    forums: dict[int, int] = {}
    # lofi pages have no absolute numbering, so page N's posts start after page N-1's.
    lofi_page_counts: dict[tuple[int, int], int] = {}

    thread_pages = []
    for path in files:
        for regex, kind in ((RE_LOFI, "lofi"), (RE_FULL, "showthread")):
            m = regex.match(path.name)
            if m:
                thread_pages.append((int(m.group(1)), page_no(m.group(2)), kind, path))
                break

    # lofi first, so a showthread post can overwrite the lofi one for the same slot.
    thread_pages.sort(key=lambda row: (row[0], row[2] != "lofi", row[1]))

    seen = 0
    for tid, page, kind, path in thread_pages:
        text = read_page(path)
        title, forum_id, posts = (parse_lofi if kind == "lofi" else parse_full)(text, page)
        if title and (tid not in titles or kind == "showthread"):
            titles[tid] = title
        if forum_id:
            forums[tid] = forum_id
        if kind == "lofi":
            lofi_page_counts[(tid, page)] = len(posts)
            offset = sum(
                count for (t, p), count in lofi_page_counts.items() if t == tid and p < page
            )
            for post in posts:
                post["seq"] += offset
        for post in posts:
            key = (tid, post["seq"])
            old = collected.get(key)
            # showthread wins on equal footing (it carries the post id and member id),
            # but a whole body always beats one the snapshot cut in half.
            better = old is None or (
                (old["truncated"], old["source"] == "lofi")
                > (post["truncated"], post["source"] == "lofi")
            )
            if better:
                collected[key] = post
        seen += 1
        if seen % 1000 == 0:
            print(f"  {seen}/{len(thread_pages)} pages, {len(collected)} posts", file=sys.stderr)
        if args.limit and seen >= args.limit:
            break

    for tid in sorted(set(titles) | set(forums) | {t for t, _ in collected}):
        db.execute(
            "INSERT OR REPLACE INTO threads (id, forum_id, title) VALUES (?, ?, ?)",
            (tid, forums.get(tid) or thread_forum.get(tid), titles.get(tid)),
        )

    # Some snapshots carry the same thread rendered several times over (t-343
    # holds every post five times): same author, same minute, same body is the
    # snapshot stuttering, not a poster double-posting. 686 posts across 62
    # threads. Dropped here, and the survivors renumbered so `seq` stays 1..N —
    # it is the anchor in the rendered page.
    rows, dropped = [], 0
    for tid in sorted({t for t, _ in collected}):
        seen_body: set[tuple[str | None, str | None, str]] = set()
        seq = 0
        for (_t, _s), post in sorted(
            ((k, v) for k, v in collected.items() if k[0] == tid)
        ):
            key = (post["username"], post["posted_at"],
                   hashlib.md5((post["body_text"] or "").encode()).hexdigest())
            if key in seen_body:
                dropped += 1
                continue
            seen_body.add(key)
            seq += 1
            rows.append(dict(post, thread_id=tid, seq=seq))
    print(f"deduped: {dropped} repeated posts dropped", file=sys.stderr)

    db.executemany(
        """INSERT INTO posts
             (thread_id, seq, page, vb_post_id, username, member_id, posted_at,
              body_html, body_text, source, truncated)
           VALUES (:thread_id, :seq, :page, :vb_post_id, :username, :member_id,
                   :posted_at, :body_html, :body_text, :source, :truncated)""",
        rows,
    )

    reordered = reorder_by_post_id(db)
    print(f"reordered: {reordered} posts put back in the board's own order", file=sys.stderr)

    # Forums referenced only by a breadcrumb have no index page; keep the id anyway.
    db.execute(
        "INSERT OR IGNORE INTO forums (id, name) SELECT DISTINCT forum_id, NULL "
        "FROM threads WHERE forum_id IS NOT NULL"
    )
    db.execute(
        "UPDATE threads SET post_count = (SELECT count(*) FROM posts WHERE thread_id = threads.id), "
        "first_post_at = (SELECT min(posted_at) FROM posts WHERE thread_id = threads.id), "
        "last_post_at  = (SELECT max(posted_at) FROM posts WHERE thread_id = threads.id)"
    )
    db.execute(
        "UPDATE forums SET thread_count = "
        "(SELECT count(*) FROM threads WHERE threads.forum_id = forums.id)"
    )
    db.execute("INSERT INTO posts_fts(rowid, username, body_text) SELECT id, username, body_text FROM posts")
    db.commit()

    stats = db.execute(
        "SELECT (SELECT count(*) FROM forums), (SELECT count(*) FROM threads), "
        "(SELECT count(*) FROM posts), (SELECT count(*) FROM posts WHERE source='showthread'), "
        "(SELECT count(*) FROM posts WHERE posted_at IS NULL), "
        "(SELECT min(posted_at) FROM posts), (SELECT max(posted_at) FROM posts)"
    ).fetchone()
    db.close()

    print(
        f"done: {stats[0]} forums, {stats[1]} threads, {stats[2]} posts "
        f"({stats[3]} from showthread), {stats[4]} without a date, "
        f"span {stats[5]} .. {stats[6]} -> {args.db}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
