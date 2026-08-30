# Azzurra forum archive

A rescue of `forum.azzurra.org` (vBulletin, 2001-06-28 → 2016-07-29) from the Wayback
Machine, plus the tooling that pulled it and the importer that turns it into a queryable
database. The forum itself is long gone; what is here is everything the Archive still had.

## What is in the repo

| Path | What it is |
|------|-----------|
| `pages/` | 8695 raw HTML snapshots as fetched, byte-for-byte, ISO-8859-1. **The irreplaceable part.** |
| `forum_import.py` | HTML → SQLite importer. Rebuilds `forum.db` from scratch on every run (~30 s). |
| `slow_get*.sh`, `batch_get*.sh`, `get_one.sh`, `retry_snaps.sh` | The fetchers, in the order they were written. `slow_get.sh` is the one that finally worked: long cooldown, resumable, skips files already full. |
| `fetch_cdx*.sh`, `cdx_*.t*` | Wayback CDX index queries and their output — the target lists came from here. |
| `targets*.tsv` | The fetch worklists, one line per URL, newest snapshot first with older ones as fallback. |
| `*.log` | Fetch logs, kept as the record of what was tried and what the Archive refused. |
| `build_index.py` | The first, throwaway indexer. Superseded by `forum_import.py`. |

`forum.db` is **not** in the repo: 169 MB (over GitHub's 100 MB per-file limit) and
regenerated from `pages/` in half a minute.

## Working on it

Four steps, in this order. Steps 1-3 only matter while there are still holes to close;
step 4 is the one you run after every parser change.

```sh
# 1. list what the Wayback Machine has (already done; the output is in cdx_*.t*)
./fetch_cdx_full.sh

# 2. fetch, politely. One request at a time, resumable, backs off when refused.
DELAY=4 COOL=240 nohup ./slow_get.sh > slow_get.log 2>&1 &

# 3. second pass for threads that imported empty: every snapshot of both markups,
#    downloaded into retry/, then the best candidate promoted into pages/
DELAY=4 COOL=240 nohup ./retry_zero.sh > slow_get_zero.log 2>&1 &
python3 pick_zero.py --dry-run     # look first
python3 pick_zero.py               # then promote

# 4. rebuild the database from whatever is on disk (~30 s, from scratch every time)
python3 forum_import.py
```

**Never run two fetchers at once**, and check before starting one:

```sh
ps -eo pid,cmd | grep '[s]low_get'      # `pgrep -f` matches its own grep — do not
```

Rebuilding costs half a minute, so a parser fix is cheap to test: change, re-run, count.

```sh
sqlite3 forum.db "SELECT count(*) FROM threads WHERE post_count = 0"
sqlite3 forum.db "SELECT count(*) FROM posts WHERE truncated = 1"
```

Current output: **99 forums, 6565 threads, 154086 posts**, 22 posts without a date,
span `2001-06-28T22:29` → `2016-07-29T16:07`.

Schema: `forums` / `threads` / `posts`, plus a `posts_fts` FTS5 index over username and
body text (`unicode61 remove_diacritics 2`). Every post keeps both `body_html` and
`body_text`; `source` says which markup it came from.

## Three markups, one forum

Ten years of vBulletin skins are in here, and the importer parses all of them:

- **lofi archive** (`t-N.html`) — `div.post` > `div.username` / `div.date` / `div.posttext`.
  No post ids, so `seq` is the position on the page.
- **full showthread** (`st-N.html`) — `table#postN`, `a.bigusername`, `id="postcountN"`.
  Carries the real post id and member id, so it wins over lofi for the same slot.
- **`azzurra2.0`, 2001-2003** (low thread ids) — a showthread skin with no
  `<!-- status icon and date -->` delimiters; the date sits bare in `td.thead`.

## Truncated snapshots

Many snapshots were saved half-written: the head is there, the last `posttext` never gets
its closing `</div>`. The importer accepts a body that ends at EOF and sets `posts.truncated
= 1` on it — 775 posts today. Half a post from 2001 beats none.

```sql
SELECT count(*) FROM posts WHERE truncated = 1;
```

## Known holes

- **914 threads have a page on disk and zero posts**: the snapshot was cut *before* the
  body, so the file holds only the `<head>` and the navbar. Nothing to parse — these need
  alternate snapshots refetched, which is scrape work, not parser work.
- **12 threads are gone for good**: `t-{3009,3222,6976,7382,8149,8286,8438,8439,10312,11104,11198,12455}`.
  Every snapshot the Archive lists for them returns an empty body.

## Lessons learned

Every one of these cost real time. They are written down so the next pass does not buy
them again.

**The Archive punishes parallelism, silently.** The first attempt ran batches in
parallel and looked like it worked: HTTP 200 for everything. About 2360 of those 200s
had a **zero-length body** — rate-limiting that does not announce itself as an error.
Serial fetching with a delay and a cooling-off period got a 100% yield on the same list.
`curl` exit 7 is the honest refusal; an empty 200 is the dishonest one, and only the
second is dangerous, because `rc=0` reads as success.

**A non-empty file is not a complete file.** The fetchers skip a target that already has
bytes on disk, which is right for resuming and wrong for repairing: the 914 threads that
imported empty all *had* a page. Repairing needs a separate output directory, not a
resumed run.

**We did not truncate those pages — the Archive did.** Refetching them returns byte-for-
byte identical files (`t-10` 1284, `t-12` 1386, `t-2` 1580). The instinct to blame our own
transfer was wrong, and one refetch measured it in a minute. What rescues the content is
the *other* markup: `showthread` for the same thread is 30 KB and parses posts where the
lofi snapshot yields none. **When one form of a page is damaged, go looking for another
form of the same page** before declaring it lost.

**`grep -c` counts lines, and these files are one line.** It reported 2 `bigusername` in a
60 KB page that actually held ten posts, which sent the investigation chasing an imaginary
"third markup" for an hour. On single-line HTML use `grep -o … | wc -l`. And these pages
are ISO-8859-1, so `grep` treats them as binary and skips them *without saying so* —
always `grep -a`. **A measurement that lies is worse than no measurement.**

**Strict parsing throws away recoverable data.** Requiring the `<!-- status icon and date -->`
delimiters zeroed 1939 threads whose pages were perfectly readable in an older skin;
requiring the closing `</div>` threw away every snapshot the Archive had cut mid-body.
Both fixes were two characters of regex and together recovered ~16k posts. Parse what is
there, record what is missing (`posts.truncated`), and let the reader decide.

**Score candidates by parsing them, not by size.** A head-only truncation can be larger
than a short but complete thread. `pick_zero.py` runs the real parsers and ranks by posts
extracted, ties broken by whole bodies — the only measure that matches what you want.

**The database is disposable, the pages are not.** `forum.db` is rebuilt from scratch in
half a minute, so a parser change costs one command and nothing is precious about the
output. `pages/` is the opposite: twelve threads are already gone for good, and no amount
of code brings them back.
