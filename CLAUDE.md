# CLAUDE.md — working on this archive

Operating notes for an agent (or a human) touching this repository. The README is the
tour in Italian; this file is the set of rules that keep a session from wasting hours.

## What this repo is

A rescue of `forum.azzurra.org` (vBulletin, 2001-2016) from the Wayback Machine. Three
kinds of thing live here, and they are not equally valuable:

1. **`pages/`, `retry/`, `assets/`, `smilies/` — the archaeology.** Bytes fetched from the
   Archive over hours of deliberately slow crawling. Twelve threads are already gone for
   good. Treat these as write-once: never rewrite, never "clean up", never re-fetch what
   is already on disk.
2. **The scripts** — fetchers, importer, renderer. Disposable in principle, but the
   fetchers encode hard-won politeness rules; read the comments before changing them.
3. **`forum.db` and `site/` — build artifacts**, both gitignored. Rebuilt from the pages
   in seconds. Nothing about them is precious.

The rendered result is published at <https://sindro.me/t/forum-azzurra/>.

## The pipeline

```sh
python3 forum_import.py                              # pages/ -> forum.db     (~30 s)
python3 forum_render.py                              # forum.db -> site/      (~20 s, 6706 pages)
./bin/pagefind --site site --output-subdir pagefind   # search index          (6565 pages, 249507 words)
```

Fetching is a separate, much slower world (`slow_get.sh`, `retry_zero.sh`,
`fetch_assets.sh`) and is only needed when there are still holes to close.

## Hard rules

- **Never run two fetchers at the same time.** One consumer on the Archive, ever. Check
  with `ps -eo pid,cmd | grep '[s]low_get'` — `pgrep -f` matches its own grep and will
  cheerfully tell you a fetcher is running when none is.
- **Never fetch in parallel.** The Archive rate-limits by returning `HTTP 200` with a
  zero-length body. `rc=0` reads as success and the corruption is silent. Serial, with
  `DELAY` and `COOL`, yields 100% on the same list.
- **Never delete or overwrite anything under `pages/`, `retry/`, `assets/`, `smilies/`.**
  If a page needs repairing, fetch the alternative into a *separate* directory and promote
  it with `pick_zero.py`. A resumed fetcher skips files that already have bytes, so
  "repair by re-running the fetcher" does nothing at all.
- **Never hand-edit `site/`.** It is regenerated wholesale; edits go into
  `forum_render.py`.
- **These files are ISO-8859-1.** Always `grep -a`, or `grep` decides they are binary and
  skips them without saying so. The importer decodes once, on read; nothing downstream
  should think about encoding again.
- **These files are one line long.** `grep -c` counts lines, so it will report 2 where
  there are 10. Use `grep -o … | wc -l`.
- **Query the CDX index as text**: `output=text&fl=timestamp`. With `output=json` the
  timestamp sits after a comma, and a field-extraction written for the old shape returns
  empty for every URL — which reads as "the Archive has nothing" when in fact it has
  everything.

## Measure, then claim

Every number in the README came from a query, and it must stay that way. Before saying a
fix worked:

```sh
sqlite3 forum.db "SELECT count(*) FROM posts"                       -- 154198
sqlite3 forum.db "SELECT count(*) FROM posts WHERE truncated = 1"   -- 771 real cuts
sqlite3 forum.db "SELECT count(*) FROM threads WHERE post_count=0"  -- 764 head-only snapshots
sqlite3 forum.db "SELECT source, count(*) FROM posts GROUP BY source"
```

A rebuild costs half a minute, so there is never an excuse for an estimate. If a claim
cannot be turned into a query, it is not a claim yet.

Two specific traps that produced confident and wrong answers before:

- **Measuring the DB when the question was about the rendered site.** They diverge the
  moment you change the renderer without re-rendering. Check the artifact the question is
  actually about.
- **Believing a bug report or disbelieving one without reproducing it.** Both truncated
  quotes and dead intra-forum links were reported by readers, dismissed once, and turned
  out to be real parser bugs.

## Parser lore

- **Two markups, one forum.** Lo-fi (`t-N.html`) has no post ids, so `seq` is the position
  on the page; full showthread (`st-N.html`) carries the real vB post and member ids and
  wins for the same `(thread, seq)`. A third variant, the 2001-2003 `azzurra2.0` skin, has
  no `<!-- status icon and date -->` delimiters and keeps the date bare in `td.thead`.
- **Post bodies contain nested `<div>`s.** vBulletin renders quotes and code blocks as
  divs, so a non-greedy `.*?</div>` cuts the body at the first quote. Close the body by
  counting nesting.
- **vBulletin duplicates every post in a `pd[N] = '...'` JavaScript block** with escaped
  newlines and HTML comments split across string concatenations. Do not let the parser
  find posts in there.
- **Parse loosely, record the damage.** Requiring well-formed markup threw away thousands
  of readable posts. Accept a body that ends at EOF and flag it with `posts.truncated = 1`;
  half a post from 2001 beats none.
- **Post bodies are untrusted markup.** The renderer strips `<script>`, `<style>`,
  `<iframe>`, `<object>`, `<embed>`, every `on*=` handler and every `javascript:` URL —
  including the unclosed variants an Archive cut leaves behind. Everything else passes
  through verbatim: the `<font>` tags are part of the record.

## Style

- Output is plain static HTML: relative `<a href>` everywhere, one `index.html` per
  directory, `wget -r` walks the whole thing. Search is the only JavaScript on the site.
- Comments explain *why*, and cite the measurement that motivated them. Several
  non-obvious lines here exist because of a specific bug; deleting the comment invites its
  return.
- Commit messages are in English, written in the imperative, and say what changed in the
  data as well as in the code ("stop cutting posts in half", not "fix regex").
- This is a public repository about someone else's community: keep the register neutral in
  code, comments, commits and issues.
