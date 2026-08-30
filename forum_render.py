#!/usr/bin/env python3
"""forum_render.py — static viewer generator for the Azzurra forum archive.

Reads forum.db (built by forum_import.py) and writes a self-contained static
site: one directory per forum, one per thread, plus an index, a sitemap and a
search page wired to Pagefind's own UI.

Design notes worth keeping:

  * The DB is the truth, the HTML is disposable. Re-render is cheap (seconds);
    never hand-edit the output.

  * URLs are `forum/<id>-<slug>/` and `thread/<id>-<slug>/`, served as
    `index.html`. The numeric id keeps them unique when two threads share a
    title, the slug keeps them readable. Every link in the output is a plain
    `<a href>` to a relative path: `wget -r` walks the whole archive and no
    JavaScript is needed to navigate — search is the only JS on the site.

  * Post bodies are 2001-2016 vBulletin HTML. They are NOT trusted markup:
    the sanitiser drops <script>/<iframe>/<object>/<embed>/<style>, every
    `on*=` handler and every `javascript:` URL. Everything else passes through
    verbatim — this is an archive, the ugly <font> tags are part of the record.

  * Smilies were served from the old board's own /images/smilies/ tree, which
    the Archive never captured. Those <img> become their alt text so a post
    does not turn into a row of broken-image icons.

  * Thread pages carry `data-pagefind-body` so the index covers the posts and
    not the navigation chrome.
"""

from __future__ import annotations

import argparse
import html
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------- sanitising

RE_BAD_BLOCK = re.compile(
    r"<\s*(script|style|iframe|object|embed|applet)\b.*?<\s*/\s*\1\s*>",
    re.I | re.S,
)
# ...and the unclosed variants: an Archive snapshot cut mid-tag leaves them.
RE_BAD_OPEN = re.compile(r"<\s*/?\s*(script|style|iframe|object|embed|applet)\b[^>]*>", re.I)
RE_ON_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
RE_JS_URL = re.compile(r"((?:href|src)\s*=\s*[\"']?)\s*javascript:[^\"'>\s]*", re.I)
RE_SMILEY = re.compile(r"<img[^>]*src=\"[^\"]*images/smilies/[^\"]*\"[^>]*>", re.I)
RE_ALT = re.compile(r"alt=\"([^\"]*)\"", re.I)
RE_TITLE = re.compile(r"title=\"([^\"]*)\"", re.I)


def _smiley_to_text(m: re.Match[str]) -> str:
    tag = m.group(0)
    for rx in (RE_ALT, RE_TITLE):
        hit = rx.search(tag)
        if hit and hit.group(1).strip():
            return f'<span class="smiley">{hit.group(1)}</span>'
    return '<span class="smiley">*</span>'


def sanitise(body: str) -> str:
    body = RE_BAD_BLOCK.sub("", body)
    body = RE_BAD_OPEN.sub("", body)
    body = RE_ON_ATTR.sub("", body)
    body = RE_JS_URL.sub(r"\1#", body)
    return RE_SMILEY.sub(_smiley_to_text, body)


# ------------------------------------------------------------------ helpers

RE_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slug(text: str | None, fallback: str = "discussione") -> str:
    """`Il Manabile di #altrove` -> `il-manabile-di-altrove`."""
    s = unicodedata.normalize("NFKD", text or "")
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = RE_SLUG_BAD.sub("-", s).strip("-")
    if len(s) > 60:
        s = s[:60].rsplit("-", 1)[0] or s[:60]
    return s or fallback


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def when(iso: str | None) -> str:
    """`2004-09-20T12:13` -> `20/09/2004 12:13`. Unknown stays unknown."""
    if not iso or len(iso) < 10:
        return "data ignota"
    d, _, t = iso.partition("T")
    y, m, dd = d.split("-")
    return f"{dd}/{m}/{y}" + (f" {t[:5]}" if t else "")


CSS = """\
:root{--bg:#faf9f7;--fg:#1b1b1b;--dim:#6a6a6a;--line:#ddd9d2;--acc:#7a1f1f;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e6e3de;--dim:#9a948c;
--line:#2e2c29;--acc:#e0a0a0;--card:#1c1b1a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:52rem;margin:0 auto;padding:1.2rem 1rem 4rem}
header.top{border-bottom:1px solid var(--line);margin-bottom:1.4rem;padding-bottom:.8rem;
display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap}
header.top h1{font-size:1.15rem;margin:0 0 .2rem}
header.top .crumb{font-size:.85rem;color:var(--dim)}
header.top .find{font-size:.85rem;white-space:nowrap}
h2.tt{font-size:1.35rem;line-height:1.3;margin:.2rem 0 .3rem}
p.meta{font-size:.85rem;color:var(--dim)}
ul.list{list-style:none;margin:0;padding:0}
ul.list li{border-bottom:1px solid var(--line);padding:.55rem 0}
ul.list .meta{font-size:.8rem;color:var(--dim)}
article.post{background:var(--card);border:1px solid var(--line);border-radius:6px;
margin:0 0 .9rem;padding:.7rem .85rem;overflow-wrap:anywhere}
article.post header{font-size:.85rem;color:var(--dim);margin-bottom:.45rem;
border-bottom:1px solid var(--line);padding-bottom:.35rem}
article.post header .who{color:var(--fg);font-weight:600}
article.post .body{overflow-x:auto}
article.post img{max-width:100%;height:auto}
.smiley{color:var(--dim);font-size:.85em}
.trunc{font-size:.8rem;color:var(--acc);margin-top:.4rem}
.pager{margin:1.2rem 0;font-size:.9rem}
.pager a,.pager span{display:inline-block;padding:.15rem .45rem}
.pager .cur{background:var(--line);border-radius:4px}
footer.foot{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:.8rem;
font-size:.8rem;color:var(--dim)}
table{max-width:100%;display:block;overflow-x:auto}
"""

PAGE = """\
<!doctype html>
<html lang="it"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="stylesheet" href="{root}style.css">
{extra}</head>
<body><div class="wrap">
<header class="top"><div><h1><a href="{root}">Archivio forum Azzurra</a></h1>
<div class="crumb">{crumb}</div></div>
<div class="find"><a href="{root}cerca/">cerca</a></div></header>
{body}
<footer class="foot">{foot}</footer>
</div></body></html>
"""

FOOT = ('Archivio dei forum di Azzurra, ricostruito dagli snapshot di '
        '<a href="https://web.archive.org/">Internet Archive</a>. '
        'I contenuti sono dei rispettivi autori.')

THREADS_PER_PAGE = 100


def write(path: Path, *, title: str, crumb: str, body: str, root: str,
          desc: str = "", extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PAGE.format(title=esc(title), crumb=crumb, body=body, root=root,
                    foot=FOOT, desc=esc(desc[:180]), extra=extra),
        encoding="utf-8",
    )


def db_download_note(out: Path) -> str:
    """Offer the SQLite database on the index, but only once it is next to it.

    The compressed DB is dropped into the output directory by hand (it is not a
    render product), so the paragraph appears when the file is there and stays
    quiet when it is not — no dead link on a freshly rendered tree.
    """
    blob = out / "forum.db.zst"
    if not blob.exists():
        return ""
    mb = blob.stat().st_size / 1e6
    return ('<p class="meta">Lo stesso archivio sta in un database SQLite, con la '
            'ricerca full-text gia\' dentro: <a href="forum.db.zst">forum.db.zst</a>, '
            f'{mb:.0f} MB compressi. <code>zstd -d forum.db.zst</code> e poi ci si '
            'parla in SQL: tabelle <code>forums</code>, <code>threads</code>, '
            '<code>posts</code> e l\'indice <code>posts_fts</code>.</p>')


def pager(pages: int, cur: int, root_up: str, base: str) -> str:
    if pages < 2:
        return ""
    out = ['<div class="pager">']
    for p in range(1, pages + 1):
        href = f"{root_up}{base}/" if p == 1 else f"{root_up}{base}/page-{p}/"
        out.append(f'<span class="cur">{p}</span>' if p == cur
                   else f'<a href="{href}">{p}</a>')
    out.append("</div>")
    return "".join(out)


# -------------------------------------------------------------------- render

def render(db_path: Path, out: Path, base_url: str) -> None:
    t0 = time.time()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    out.mkdir(parents=True, exist_ok=True)
    (out / "style.css").write_text(CSS, encoding="utf-8")

    forums = db.execute(
        "SELECT id, name, thread_count FROM forums ORDER BY thread_count DESC, id"
    ).fetchall()
    fslug = {f["id"]: f"{f['id']}-{slug(f['name'], 'forum')}" for f in forums}
    fname = {f["id"]: (f["name"] or f"forum {f['id']}") for f in forums}

    urls: list[str] = [""]
    n_files = 0

    # --- index -------------------------------------------------------------
    n_threads, n_posts = db.execute(
        "SELECT (SELECT count(*) FROM threads), (SELECT count(*) FROM posts)"
    ).fetchone()
    span = db.execute(
        "SELECT min(posted_at), max(posted_at) FROM posts WHERE posted_at IS NOT NULL"
    ).fetchone()
    rows = "".join(
        f'<li><a href="forum/{fslug[f["id"]]}/">{esc(fname[f["id"]])}</a>'
        f'<div class="meta">{f["thread_count"]} discussioni</div></li>'
        for f in forums
    )
    write(out / "index.html", title="Archivio forum Azzurra",
          crumb="indice dei forum", root="",
          desc=f"I forum storici di Azzurra: {n_threads} discussioni e "
               f"{n_posts} messaggi dal 2001 al 2016.",
          body=(f'<p class="meta">{len(forums)} forum, {n_threads} discussioni, '
                f'{n_posts} messaggi, dal {when(span[0])} al {when(span[1])}.</p>'
                f'<ul class="list">{rows}</ul>'
                + db_download_note(out)))
    n_files += 1

    # --- forums -------------------------------------------------------------
    for f in forums:
        threads = db.execute(
            "SELECT id, title, post_count, first_post_at, last_post_at "
            "FROM threads WHERE forum_id = ? "
            "ORDER BY coalesce(last_post_at, first_post_at) DESC, id DESC",
            (f["id"],),
        ).fetchall()
        pages = max(1, -(-len(threads) // THREADS_PER_PAGE))
        for p in range(1, pages + 1):
            chunk = threads[(p - 1) * THREADS_PER_PAGE: p * THREADS_PER_PAGE]
            up = "../../" if p == 1 else "../../../"
            items = "".join(
                f'<li><a href="{up}thread/{t["id"]}-{slug(t["title"])}/">'
                f'{esc(t["title"] or f"discussione {t['id']}")}</a>'
                f'<div class="meta">{t["post_count"]} messaggi &middot; '
                f'{when(t["first_post_at"])} &rarr; {when(t["last_post_at"])}</div></li>'
                for t in chunk
            )
            rel = f"forum/{fslug[f['id']]}/" + ("" if p == 1 else f"page-{p}/")
            write(out / rel / "index.html",
                  title=f"{fname[f['id']]} — Archivio forum Azzurra",
                  crumb=f'<a href="{up}">forum</a> &rsaquo; {esc(fname[f["id"]])}',
                  root=up,
                  desc=f"{f['thread_count']} discussioni del forum "
                       f"{fname[f['id']]} di Azzurra.",
                  body=(f'<h2 class="tt">{esc(fname[f["id"]])}</h2>'
                        f'<ul class="list">{items}</ul>'
                        + pager(pages, p, up, f"forum/{fslug[f['id']]}")))
            urls.append(rel)
            n_files += 1

    # --- threads ------------------------------------------------------------
    threads = db.execute(
        "SELECT id, title, forum_id, post_count FROM threads ORDER BY id"
    ).fetchall()
    empty = 0
    for t in threads:
        posts = db.execute(
            "SELECT seq, username, posted_at, body_html, body_text, truncated "
            "FROM posts WHERE thread_id = ? ORDER BY seq",
            (t["id"],),
        ).fetchall()
        title = t["title"] or f"discussione {t['id']}"
        blocks = []
        for p in posts:
            trunc = ('<div class="trunc">[lo snapshot dell\'Archive si interrompe '
                     "qui: il messaggio e' incompleto]</div>" if p["truncated"] else "")
            blocks.append(
                f'<article class="post" id="post-{p["seq"]}">'
                f'<header><span class="who">{esc(p["username"] or "anonimo")}</span>'
                f' &middot; {when(p["posted_at"])} &middot; '
                f'<a href="#post-{p["seq"]}">#{p["seq"]}</a></header>'
                f'<div class="body">{sanitise(p["body_html"])}</div>{trunc}</article>'
            )
        if not blocks:
            empty += 1
            blocks.append('<article class="post"><div class="body"><em>'
                          "Di questa discussione l'Archive ha salvato solo "
                          "l'intestazione: nessun messaggio recuperabile."
                          "</em></div></article>")
        crumb = '<a href="../../">forum</a>'
        if t["forum_id"] in fslug:
            crumb += (f' &rsaquo; <a href="../../forum/{fslug[t["forum_id"]]}/">'
                      f'{esc(fname[t["forum_id"]])}</a>')
        rel = f"thread/{t['id']}-{slug(title)}/"
        write(out / rel / "index.html",
              title=f"{title} — Archivio forum Azzurra", crumb=crumb, root="../../",
              desc=(posts[0]["body_text"][:180] if posts else
                    f"Discussione {t['id']} dei forum di Azzurra."),
              body=(f'<h2 class="tt">{esc(title)}</h2>'
                    f'<div data-pagefind-body>{"".join(blocks)}</div>'))
        urls.append(rel)
        n_files += 1

    # --- search page (Pagefind's own UI) ------------------------------------
    write(out / "cerca" / "index.html", title="Cerca — Archivio forum Azzurra",
          crumb='<a href="../">forum</a> &rsaquo; cerca', root="../",
          desc="Ricerca full-text nell'archivio dei forum di Azzurra.",
          extra='<link rel="stylesheet" href="../pagefind/pagefind-ui.css">',
          body=('<h2 class="tt">Cerca nell\'archivio</h2>'
                '<div id="search"></div>'
                '<noscript><p class="meta">La ricerca ha bisogno di JavaScript. '
                'Senza, si naviga dall\'<a href="../">indice dei forum</a>: '
                'ogni pagina e\' HTML statico.</p></noscript>'
                '<script src="../pagefind/pagefind-ui.js"></script>'
                '<script>window.addEventListener("DOMContentLoaded",function(){'
                'new PagefindUI({element:"#search",bundlePath:"../pagefind/",'
                'showSubResults:true,pageSize:20});});</script>'))
    n_files += 1

    # --- sitemap ------------------------------------------------------------
    base = base_url.rstrip("/") + "/"
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"<url><loc>{esc(base + u)}</loc></url>" for u in urls]
    sm.append("</urlset>")
    (out / "sitemap.xml").write_text("\n".join(sm), encoding="utf-8")
    (out / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n", encoding="utf-8")

    db.close()
    mb = sum(p.stat().st_size for p in out.rglob("*.html")) / 1048576
    print(f"DONE {n_files} pagine, {mb:.1f} MB, {empty} discussioni senza messaggi, "
          f"{len(urls)} url in sitemap, {time.time() - t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="forum.db", type=Path)
    ap.add_argument("--out", default="site", type=Path)
    ap.add_argument("--base-url", default="https://sindro.me/t/forum-azzurra/")
    a = ap.parse_args()
    render(a.db, a.out, a.base_url)


if __name__ == "__main__":
    main()
