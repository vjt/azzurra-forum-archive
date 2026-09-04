#!/usr/bin/env python3
"""Fold the phpBB mirror (`old_posts`) into the archive proper (`posts`).

The board did not change forum when it changed software: the phpBB threads of
2001-2004 and the vBulletin threads that came after are the same discussions,
so they belong in one corpus. vBulletin's own migration already carried part of
phpBB across — 4928 posts of 2001-2002 are in `posts` before this script runs —
so this is a merge with deduplication, never an append.

What it does, in order:

1. **Match** each mirror topic to a vB thread by normalised title. A unique hit
   is the match; several candidates are broken by *content* (how many
   author+body pairs the two sides share) and then by first-post date. What
   still has no thread becomes a new one, numbered `1000000 + topic_id` so a
   synthetic id can never collide with a real vB one.
2. **Name what vBulletin lost.** 66 of the 99 forums reached the crawler
   without a name. Matched threads vote for the phpBB forum they came from, and
   a forum takes that name only on decisive evidence (see NAME_MIN_VOTES /
   NAME_MIN_SHARE) — a couple of stray votes are a thread-title collision, not
   a naming.
3. **Deduplicate** against the destination thread on (author, normalised body).
   NOT on the timestamp: the two corpora disagree by a systematic hour (DST at
   migration time), and the small deltas that remain are real posts fired
   seconds apart by the same author, not copies.
4. **Interleave** the survivors chronologically. The per-thread clock offset is
   measured from the duplicate posts themselves — they are the same post seen
   twice, so their delta is the offset — and applied to the mirror timestamps
   before sorting. `seq` is then renumbered across the whole thread, which is
   what inserting older posts at the head means.

`posts.old_post_id` and `threads.old_topic_id` keep the provenance, so the
renderer can turn a `viewtopic.php?t=…&p=…` link into a local anchor instead of
sending the reader to the Wayback Machine.

    python3 oldboard_merge.py --db forum.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# A synthetic thread/forum id is a real phpBB id lifted out of vBulletin's
# range. vB's largest thread id in the archive is five digits; a million is far
# above anything the board ever numbered, and the offset keeps the id stable
# across rebuilds (an autoincrement would not).
SYNTHETIC_BASE = 1_000_000

# Naming a forum from votes is only safe when the evidence is not a coincidence:
# a handful of threads with a common title ("Regolamento") match everywhere.
NAME_MIN_VOTES = 5
NAME_MIN_SHARE = 0.8

# When several vB threads share a title and none of them shares a single post
# with the mirror topic, the only evidence left is when the discussion started.
# Beyond a year apart that is not evidence: two different threads called
# "Regolamento". Such a topic becomes a thread of its own — nothing is lost,
# it simply is not stitched. A head-only thread (a title vBulletin kept with no
# post behind it) is exempt: it has no date to compare and no content to
# contradict, and giving it the mirror's posts is the whole point of this merge.
MATCH_MAX_GAP_DAYS = 365

RE_NOISE = re.compile(r"[^a-z0-9]+")


def norm_title(s: str | None) -> str:
    """Title reduced to what survives a software migration: letters and digits.

    vBulletin re-encoded accents, changed quoting and collapsed whitespace when
    it imported phpBB, so a byte comparison matches almost nothing.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return RE_NOISE.sub(" ", s.lower()).strip()


def norm_body(s: str | None) -> str:
    """Body reduced for equality: the same post rendered by two forums.

    Signatures are cut (phpBB fences them with `___`, vBulletin dropped them),
    smilie image names and quote decorations differ, so only the letters and
    digits of the text are compared.
    """
    if not s:
        return ""
    s = s.split("\n___")[0]
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return RE_NOISE.sub(" ", s.lower()).strip()


def norm_user(s: str | None) -> str:
    return RE_NOISE.sub("", (s or "").lower())


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def tokens(s: str | None) -> set[str]:
    return set(norm_body(s).split())


def same_post(a: set[str], b: set[str]) -> bool:
    """Is this the same post rendered by two different forums?

    Equality is too strict, measured: vBulletin's lo-fi view keeps the smilie
    as the literal `:lol:` where phpBB served an image and left nothing in the
    text, and phpBB's own signature block rides along in some bodies. So the
    shorter text has to be almost entirely inside the longer one, and the two
    must still overlap substantially — containment alone would call every
    one-word "quoto" a copy of every other.
    """
    if not a or not b:
        return False
    inter = len(a & b)
    return inter / min(len(a), len(b)) >= 0.8 and inter / len(a | b) >= 0.5


# How far the two clocks may sit apart once the corpus offset is taken out.
# The mirror stamps to the minute and vBulletin to the minute, so this is
# rounding slack, not a search window.
NEAR_SECONDS = 180
# The offsets a duplicate may sit at. Measured across the corpus: 0, +1h, -1h —
# the migration crossed a DST change. They are tried PER POST, not per thread:
# a thread that ran from March to November holds duplicates at two different
# offsets, and picking one offset for the whole thread left 104 copies standing
# (/thread/421-forum-azzurranet-e-bestemmie/ is one of them).
CANDIDATE_OFFSETS = (0, 3600, -3600)
# Below this many words a body is not an identity: "quoto", "ok anche per me".
# Two posts that short are the same post only if the author says so too.
SHORT_TOKENS = 5


class _Offsets:
    """The clock offset as a function of when the post was written.

    One number per thread is wrong for a thread that crossed the DST change: the
    March posts sit an hour off the November ones. Each mirror post is placed
    with the offset measured at the duplicate nearest to it in time.
    """

    def __init__(self, twins: list[tuple[datetime, float]], median: float) -> None:
        self._twins = twins
        self._median = median

    def at(self, ts: datetime | None) -> timedelta:
        if ts is None or not self._twins:
            return timedelta(seconds=self._median)
        near = min(self._twins, key=lambda t: abs((t[0] - ts).total_seconds()))
        return timedelta(seconds=near[1])


def dedup(existing: list, rows: list) -> tuple[_Offsets, list, int]:
    """Split the mirror's posts into (clock offsets, new posts, duplicates).

    A mirror post is a copy of one already in the thread when the bodies match
    and the two stamps sit one candidate offset apart. The author is evidence,
    not a key: vBulletin's importer rewrote nicks it could not spell — phpBB's
    `C|ty_Hunter` is vB's `City_Hunter`, `_theone_` is `theo` — and keying on
    the name left 175 copies of a post standing next to the original.
    """
    pool = [
        (parse_ts(e["posted_at"]), norm_user(e["username"]),
         tokens(e["body_text"]), e["id"])
        for e in existing
    ]
    used: set[object] = set()
    fresh: list = []
    twins: list[tuple[datetime, float]] = []
    for r in rows:
        ts = parse_ts(r["posted_at"])
        user = norm_user(r["username"])
        toks = tokens(r["body_text"])
        best: tuple[tuple[int, float], object, float] | None = None
        for ets, euser, etoks, eid in pool:
            if eid in used or not ets or not ts:
                continue
            delta = (ets - ts).total_seconds()
            if all(abs(delta - c) > NEAR_SECONDS for c in CANDIDATE_OFFSETS):
                continue
            if not same_post(toks, etoks):
                continue
            same_user = euser == user
            if not same_user and min(len(toks), len(etoks)) < SHORT_TOKENS:
                continue
            # Same author beats a rewritten one, and the smaller offset beats
            # the larger: both say "this is the copy" more loudly.
            rank = (0 if same_user else 1, abs(delta))
            if best is None or rank < best[0]:
                best = (rank, eid, delta)
        if best is None:
            fresh.append(r)
        else:
            used.add(best[1])
            twins.append((ts, best[2]))

    twins.sort()
    median = sorted(d for _, d in twins)[len(twins) // 2] if twins else 0.0
    return _Offsets(twins, median), fresh, len(twins)


def ensure_columns(db: sqlite3.Connection) -> None:
    """Add the provenance columns if this is a database built before the merge."""
    tcols = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
    if "old_topic_id" not in tcols:
        db.execute("ALTER TABLE threads ADD COLUMN old_topic_id INTEGER")
    pcols = {r[1] for r in db.execute("PRAGMA table_info(posts)")}
    if "old_post_id" not in pcols:
        db.execute("ALTER TABLE posts ADD COLUMN old_post_id INTEGER")


def match_topics(db: sqlite3.Connection) -> tuple[dict[int, int], Counter]:
    """Mirror topic id -> vB thread id, plus a tally of how each was decided."""
    threads = db.execute("SELECT id, forum_id, title FROM threads").fetchall()
    by_title: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for t in threads:
        by_title[norm_title(t["title"])].append(t)

    # Content fingerprints, built once: (author, body) pairs per vB thread and
    # per mirror topic. This is what breaks a tie between two threads that
    # share a title.
    thread_keys: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for r in db.execute("SELECT thread_id, username, body_text FROM posts"):
        thread_keys[r["thread_id"]].add((norm_user(r["username"]), norm_body(r["body_text"])))
    topic_keys: dict[int, set[tuple[str, str]]] = defaultdict(set)
    topic_first: dict[int, datetime | None] = {}
    for r in db.execute(
        "SELECT topic_id, username, body_text, posted_at FROM old_posts ORDER BY post_id"
    ):
        topic_keys[r["topic_id"]].add((norm_user(r["username"]), norm_body(r["body_text"])))
        topic_first.setdefault(r["topic_id"], parse_ts(r["posted_at"]))

    thread_first = {}
    thread_empty = set()
    for r in db.execute("SELECT id, first_post_at, post_count FROM threads"):
        thread_first[r["id"]] = parse_ts(r["first_post_at"])
        if not r["post_count"]:
            thread_empty.add(r["id"])

    how: Counter = Counter()
    mapping: dict[int, int] = {}
    taken: set[int] = set()          # one mirror topic per vB thread
    rows = db.execute("SELECT topic_id, title FROM old_topics ORDER BY topic_id").fetchall()
    # Unambiguous titles first: they claim their thread before an ambiguous
    # topic can take it by content overlap.
    for pas in (1, 2):
        for tp in rows:
            if tp["topic_id"] in mapping:
                continue
            cands = [c for c in by_title.get(norm_title(tp["title"]), []) if c["id"] not in taken]
            if not cands:
                continue
            if len(cands) == 1 and pas == 1:
                mapping[tp["topic_id"]] = cands[0]["id"]
                taken.add(cands[0]["id"])
                how["title"] += 1
            elif len(cands) > 1 and pas == 2:
                keys = topic_keys.get(tp["topic_id"], set())
                scored = []
                for c in cands:
                    shared = len(keys & thread_keys.get(c["id"], set()))
                    first = thread_first.get(c["id"])
                    mine = topic_first.get(tp["topic_id"])
                    gap = abs((first - mine).total_seconds()) if first and mine else 1 << 40
                    scored.append((-shared, gap, c["id"]))
                scored.sort()
                shared, gap, tid = scored[0]
                if shared:
                    how["content"] += 1
                elif tid in thread_empty or gap <= MATCH_MAX_GAP_DAYS * 86400:
                    how["date"] += 1
                else:
                    how["ambiguous_dropped"] += 1
                    continue
                mapping[tp["topic_id"]] = tid
                taken.add(tid)
    how["new"] = len(rows) - len(mapping)
    return mapping, how


def name_forums(db: sqlite3.Connection, mapping: dict[int, int]) -> list[tuple[int, str, int, int]]:
    """Give a name to the vB forums that reached the crawler without one."""
    old_forum = {r["topic_id"]: r["forum_id"] for r in db.execute("SELECT topic_id, forum_id FROM old_topics")}
    old_name = {r["forum_id"]: r["name"] for r in db.execute("SELECT forum_id, name FROM old_forums")}
    thread_forum = {r["id"]: r["forum_id"] for r in db.execute("SELECT id, forum_id FROM threads")}
    nameless = {r["id"] for r in db.execute("SELECT id FROM forums WHERE name IS NULL OR name = ''")}
    used = {
        norm_title(r["name"])
        for r in db.execute("SELECT name FROM forums WHERE name IS NOT NULL AND name <> ''")
    }

    votes: dict[int, Counter] = defaultdict(Counter)
    for topic_id, thread_id in mapping.items():
        fid = thread_forum.get(thread_id)
        name = old_name.get(old_forum.get(topic_id))
        if fid in nameless and name:
            votes[fid][name] += 1

    named = []
    for fid, tally in votes.items():
        name, n = tally.most_common(1)[0]
        total = sum(tally.values())
        # A name already worn by another forum is the signature of a title
        # collision — two forums with a "Regolamento" thread — not a rescue.
        if n >= NAME_MIN_VOTES and n / total >= NAME_MIN_SHARE and norm_title(name) not in used:
            db.execute("UPDATE forums SET name = ? WHERE id = ?", (name, fid))
            used.add(norm_title(name))
            named.append((fid, name, n, total))
    return sorted(named)


def merge(db: sqlite3.Connection, mapping: dict[int, int]) -> dict[str, int]:
    stats: Counter = Counter()

    old_topics = {
        r["topic_id"]: r
        for r in db.execute("SELECT topic_id, forum_id, title, source FROM old_topics")
    }
    old_forum_name = {r["forum_id"]: r["name"] for r in db.execute("SELECT forum_id, name FROM old_forums")}

    # Which vB forum does a phpBB forum correspond to? The matched threads say
    # so; a phpBB forum with no matched thread gets a synthetic forum of its own
    # rather than being dropped into a wrong one.
    thread_forum = {r["id"]: r["forum_id"] for r in db.execute("SELECT id, forum_id FROM threads")}
    fvotes: dict[int, Counter] = defaultdict(Counter)
    for topic_id, thread_id in mapping.items():
        ofid = old_topics[topic_id]["forum_id"]
        vfid = thread_forum.get(thread_id)
        if ofid is not None and vfid is not None:
            fvotes[ofid][vfid] += 1
    forum_map = {ofid: tally.most_common(1)[0][0] for ofid, tally in fvotes.items()}

    posts_by_topic: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in db.execute(
        "SELECT post_id, topic_id, seq, page, username, posted_at, subject, "
        "       body_html, body_text, source "
        # Order by the phpBB post id, not by (page, seq). The id is the board's
        # own insertion order and agrees with the clock in 8685 of 8686 rows,
        # while (page, seq) is a property of the *snapshot*: two files of the
        # same topic taken years apart carry overlapping seq numbers, and any
        # miscount of the page size zips them together instead of appending.
        "FROM old_posts ORDER BY topic_id, post_id"
    ):
        posts_by_topic[r["topic_id"]].append(r)

    new_posts: list[dict] = []
    renumber: set[int] = set()

    for topic_id, rows in posts_by_topic.items():
        thread_id = mapping.get(topic_id)
        if thread_id is None:
            # A discussion vBulletin never carried across: it arrives whole,
            # in the order phpBB itself paginated it.
            # A topic whose every snapshot lost the title header still has its
            # posts: it gets a thread with no title rather than being dropped.
            topic = old_topics.get(topic_id) or {"forum_id": rows[0]["forum_id"], "title": None}
            thread_id = SYNTHETIC_BASE + topic_id
            ofid = topic["forum_id"]
            forum_id = forum_map.get(ofid)
            if forum_id is None and ofid is not None:
                forum_id = SYNTHETIC_BASE + ofid
                db.execute(
                    "INSERT OR IGNORE INTO forums (id, name) VALUES (?, ?)",
                    (forum_id, old_forum_name.get(ofid)),
                )
            db.execute(
                "INSERT OR IGNORE INTO threads (id, forum_id, title, old_topic_id) VALUES (?,?,?,?)",
                (thread_id, forum_id, topic["title"], topic_id),
            )
            stats["threads_created"] += 1
            for i, r in enumerate(rows, 1):
                new_posts.append(
                    dict(
                        thread_id=thread_id, seq=i, page=r["page"], vb_post_id=None,
                        username=r["username"], member_id=None, posted_at=r["posted_at"],
                        body_html=r["body_html"], body_text=r["body_text"],
                        source=r["source"], truncated=0, old_post_id=r["post_id"],
                    )
                )
                stats["posts_new_thread"] += 1
            continue

        db.execute("UPDATE threads SET old_topic_id = ? WHERE id = ?", (topic_id, thread_id))
        existing = db.execute(
            "SELECT id, seq, username, posted_at, body_text FROM posts WHERE thread_id = ? ORDER BY seq",
            (thread_id,),
        ).fetchall()
        offsets, fresh, dups = dedup(existing, rows)
        stats["posts_duplicate"] += dups
        if not fresh:
            continue

        merged: list[tuple[datetime, int, object]] = []
        for e in existing:
            ts = parse_ts(e["posted_at"]) or datetime.min
            merged.append((ts, e["seq"], ("old", e["id"])))
        # The mirror's clock is the one the *old* board kept, and it sits up to
        # an hour away from vBulletin's around the migration. The post is placed
        # by the corrected stamp, so it must also be *shown* with it: storing the
        # raw one put a post at 09:53 under the 10:53 it answers, which reads as
        # a thread out of order even though the order is right.
        shifted: dict[int, str] = {}
        for i, r in enumerate(fresh):
            ts = parse_ts(r["posted_at"])
            if ts:
                ts += offsets.at(ts)
                shifted[i] = ts.isoformat(timespec="minutes")
            else:
                ts = datetime.min
            # Ties keep phpBB's own order, after everything vB already had at
            # the same minute: the mirror's minute is a rounding, not a clock.
            merged.append((ts, 1 << 20, ("new", i)))
        merged.sort(key=lambda x: (x[0], x[1]))

        for seq, (_ts, _tie, ref) in enumerate(merged, 1):
            kind, idx = ref
            if kind == "old":
                db.execute("UPDATE posts SET seq = ? WHERE id = ?", (-seq, idx))
                renumber.add(idx)
            else:
                r = fresh[idx]
                new_posts.append(
                    dict(
                        thread_id=thread_id, seq=seq, page=r["page"], vb_post_id=None,
                        username=r["username"], member_id=None,
                        posted_at=shifted.get(idx, r["posted_at"]),
                        body_html=r["body_html"], body_text=r["body_text"],
                        source=r["source"], truncated=0, old_post_id=r["post_id"],
                    )
                )
                stats["posts_merged_thread"] += 1

    # UNIQUE(thread_id, seq) forbids a renumber in place, so the moved rows sat
    # at -seq while the new ones took their final numbers; flip them back now.
    db.execute("UPDATE posts SET seq = -seq WHERE seq < 0")
    stats["posts_renumbered"] = len(renumber)

    db.executemany(
        """INSERT INTO posts
             (thread_id, seq, page, vb_post_id, username, member_id, posted_at,
              body_html, body_text, source, truncated, old_post_id)
           VALUES (:thread_id, :seq, :page, :vb_post_id, :username, :member_id,
                   :posted_at, :body_html, :body_text, :source, :truncated,
                   :old_post_id)""",
        new_posts,
    )
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="forum.db")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "old_posts" not in have:
        print("oldboard_merge: no old_posts — run oldboard_import.py first", file=sys.stderr)
        return 2
    if db.execute("SELECT count(*) FROM posts WHERE source LIKE 'phpbb%'").fetchone()[0]:
        print("oldboard_merge: already merged — rebuild with `make db`", file=sys.stderr)
        return 2

    ensure_columns(db)
    mapping, how = match_topics(db)
    print(
        f"topics: {how['title']} matched by title, {how['content']} by content, "
        f"{how['date']} by date, {how['ambiguous_dropped']} left unstitched "
        f"(same title, no shared post, over a year apart), {how['new']} new",
        file=sys.stderr,
    )

    named = name_forums(db, mapping)
    for fid, name, n, total in named:
        print(f"named forum {fid}: {name!r} ({n}/{total} votes)", file=sys.stderr)

    stats = merge(db, mapping)

    db.execute(
        "UPDATE threads SET post_count = (SELECT count(*) FROM posts WHERE thread_id = threads.id), "
        "first_post_at = (SELECT min(posted_at) FROM posts WHERE thread_id = threads.id), "
        "last_post_at  = (SELECT max(posted_at) FROM posts WHERE thread_id = threads.id)"
    )
    db.execute(
        "UPDATE forums SET thread_count = "
        "(SELECT count(*) FROM threads WHERE threads.forum_id = forums.id)"
    )
    db.execute("INSERT INTO posts_fts(posts_fts) VALUES('delete-all')")
    db.execute("INSERT INTO posts_fts(rowid, username, body_text) "
               "SELECT id, username, body_text FROM posts")
    db.commit()

    print(
        "merged: {posts_merged_thread} posts into existing threads, "
        "{posts_new_thread} into {threads_created} new threads, "
        "{posts_duplicate} already present, {posts_renumbered} renumbered".format(
            **{k: stats.get(k, 0) for k in
               ("posts_merged_thread", "posts_new_thread", "threads_created",
                "posts_duplicate", "posts_renumbered")}
        ),
        file=sys.stderr,
    )
    total, phpbb = db.execute(
        "SELECT (SELECT count(*) FROM posts), (SELECT count(*) FROM posts WHERE source LIKE 'phpbb%')"
    ).fetchone()
    print(f"posts: {total} total, {phpbb} from the phpBB mirror", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
