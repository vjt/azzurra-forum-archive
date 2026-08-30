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

## Rebuilding the database

```sh
python3 forum_import.py          # pages/ -> forum.db
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

## Gotchas, paid for once

- The pages are **ISO-8859-1**. `grep` treats them as binary and skips them silently —
  always `grep -a`.
- Most of these files are **a single line**, so `grep -c` counts 1 and means nothing. It
  is a measurement that lies; count occurrences with `grep -o | wc -l`.
- Never run two fetchers against the Archive at once. It rate-limits by host and the
  penalty is hours, not minutes.
