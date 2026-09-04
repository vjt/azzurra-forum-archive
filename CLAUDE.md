# CLAUDE.md — working on this archive

Operating notes for an agent (or a human) touching this repository. The README is the
tour in Italian; this file is the set of rules that keep a session from wasting hours.

## What this repo is

A rescue of `forum.azzurra.org` (vBulletin, 2001-2016) from the Wayback Machine. Three
kinds of thing live here, and they are not equally valuable:

1. **`pages/`, `retry/`, `assets/`, `smilies/`, `oldboard/` — the archaeology.** Bytes fetched from the
   Archive over hours of deliberately slow crawling. Twelve threads are already gone for
   good. Treat these as write-once: never rewrite, never "clean up", never re-fetch what
   is already on disk.
2. **The scripts** — fetchers, importer, renderer. Disposable in principle, but the
   fetchers encode hard-won politeness rules; read the comments before changing them.
3. **`forum.db` and `site/` — build artifacts**, both gitignored. Rebuilt from the pages
   in seconds. Nothing about them is precious.

The rendered result is published at <https://vjt.github.io/azzurra-forum-archive/>;
`sindro.me/t/forum-azzurra/` redirects there, deep links included.

## The pipeline

```sh
make                                                 # all three, in order
make db                                              # pages/ + oldboard/ -> forum.db  (~3 min)
make site                                            # forum.db -> site/      (~20 s, 7229 pages)
make search                                          # search index           (7070 pages, 256743 words)
```

CI runs the same targets (`.github/workflows/site.yml`) and publishes the result to
GitHub Pages, so the site is never committed. `bin/pagefind` in this repo is an arm64
build; CI overrides it with `make search PAGEFIND="npx -y pagefind"`.

Fetching is a separate, much slower world (`slow_get.sh`, `retry_zero.sh`,
`fetch_assets.sh`) and is only needed when there are still holes to close.

## Hard rules

- **Never run two fetchers at the same time.** One consumer on the Archive, ever. Check
  with `ps -eo pid,cmd | grep '[s]low_get'` — `pgrep -f` matches its own grep and will
  cheerfully tell you a fetcher is running when none is.
- **Never fetch in parallel.** The Archive rate-limits by returning `HTTP 200` with a
  zero-length body. `rc=0` reads as success and the corruption is silent. Serial, with
  `DELAY` and `COOL`, yields 100% on the same list.
- **Never delete or overwrite anything under `pages/`, `retry/`, `assets/`, `smilies/`,
  `oldboard/`.**
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
sqlite3 forum.db "SELECT count(*) FROM posts"                       -- 159485
sqlite3 forum.db "SELECT count(*) FROM posts WHERE truncated = 1"   -- 771 real cuts
sqlite3 forum.db "SELECT count(*) FROM threads WHERE post_count=0"  -- 596 head-only snapshots
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
- **The old board is the same forum, not a second one.** `oldboard_merge.py` stitches the
  phpBB mirror into the vBulletin threads; `make db` runs import → oldboard import → merge
  and a rebuild that stops at the first step silently loses 5629 posts.
- **Never dedup the two corpora on the timestamp alone.** They are an hour apart in places
  (the DST change around the migration) and two posts by the same user minutes apart are
  different posts. The key is the body (token containment ≥ 0.8, Jaccard ≥ 0.5) within
  180 s of one of the offsets 0/±1h, and the offset is *measured*, not assumed.
- **The author is evidence, not a dedup key.** vBulletin's import rewrote nicks it could
  not spell (`C|ty_Hunter` → `City_Hunter`, `_theone_` → `theo`), so indexing by name left
  175 copies standing. The name only confirms, and is required only under five words.
- **Measure the corpus offset per post, not per thread.** A thread that ran from March to
  November crosses the DST change and holds duplicates at two offsets; one offset for the
  whole thread left another 104 copies in place.
- **Both phpBB generations fence the post body with markup they opened outside it.** 1.4
  closes the date line's `</font>` after the first `<HR>` and draws a quote as a table with
  two more `<HR>` inside it — so the body is `rules[0]:rules[-1]`, never `rules[1]`, or 213
  posts stop at their first quote. 2.0 closes `span.postbody` before a quote table and
  reopens it after. Strip the orphan close tag at import; nothing downstream that anchors
  on the start of a body works while it is there.
- **A phpBB quote is a table, not BBCode.** `bbcode()` never sees one; `phpbb_boxes()`
  rewrites both generations (and 2.0's `td.code`) into the site's own `blockquote.bbq`.
  Read the author out of the header BEFORE recursing into the nested boxes — the other
  order lets the nick pattern eat the `<blockquote><cite>` the recursion just emitted.
- **Do not anchor a phpBB 1.4.0 parser to the row background colour.** The board was
  reskinned mid-life (`#EEEEEE`/`#aeddff` → `#F3F3F3`/`#A8CBFF`); anchoring to the first
  pair seen dropped 655 posts across 125 pages and returned zero without an error. Anchor
  to the *shape* of the row and check against the `Inviato:` count, page by page.
- **`seq` is a property of the snapshot, not of the thread.** Two copies of a page taken
  months apart disagree — a post deleted in between shifts every position after it — and
  the page size is not constant either: the old board paginated by ten (`start=` is always
  a multiple of 10, and `//15` folded `s0` and `s10` onto page 1, interleaving 72 topics),
  and the vB crawls disagree with each other. Order by the board's own post id wherever
  there is one; fall back to the clock only for threads read solely from the lo-fi view,
  where no id exists.
- **vBulletin renders the time in the reader's own format.** 460 stamps in the corpus say
  `01:21 PM`: reading the hour and dropping the marker put those posts twelve hours early.
  Any date regex here needs the optional `[AP]M`, and `12 AM` is midnight.
- **A stamp that was used for sorting must also be the one shown.** The phpBB mirror is
  placed in a vB thread with the offset measured on the nearest duplicate; storing the raw
  stamp afterwards showed a post at 09:53 under the 10:53 it answers, which reads as a bug
  even though the position is right.
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
