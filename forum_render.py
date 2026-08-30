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
    the Archive never captured — and the lo-fi pages carry no alt text, so the
    file name is all that survives. They are mapped to emoji by name, with the
    original file name kept as the tooltip; unrecognised packs stay a dim `*`
    rather than being given an invented meaning.

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
RE_SMILEY = re.compile(r"<img[^>]*src=\"[^\"]*images/smil(?:ies|es)/[^\"]*\"[^>]*>", re.I)
RE_ALT = re.compile(r"alt=\"([^\"]*)\"", re.I)
RE_TITLE = re.compile(r"title=\"([^\"]*)\"", re.I)
RE_SRC = re.compile(r"src=\"([^\"]*)\"", re.I)

# The .gif files themselves are gone, so the only thing left to go on is the
# file name.  12629 smilies across 234 distinct names, but the head is steep:
# the twenty below cover 95% of them.  Names are the board's own, in Italian
# (`muoio` = "I'm dying [laughing]", `sospiro` = sigh, `pollicesu` = thumbs up).
SMILEY_EMOJI = {
    # vBulletin / phpBB stock set
    "icon_smile": "🙂", "smile": "🙂", "smilee": "🙂", "icon_smile_big": "😃",
    "icon_biggrin": "😃", "biggrin": "😃", "icon_mrgreen": "😁",
    "icon_smile_kisses": "😘", "icon_smile_wink": "😉", "wink": "😉",
    "icon_smile_tongue": "😛", "tongue": "😛", "icon_razz": "😛",
    "linguaccia": "😛", "icon_lol": "😂", "risata": "😂", "haha": "😂",
    "muoio": "🤣", "xd": "😆", "icon_smile_cool": "😎",
    "icon_smile_angry": "😠", "mad": "😠", "icon_mad": "😠",
    "icon_smile_shock": "😲", "icon_eek": "😲", "eek": "😲",
    "icon_cry": "😢", "piangoo": "😢", "icon_sad": "🙁", "icon_smile_sad": "🙁",
    "icon_frown": "🙁", "frown": "🙁", "sospiro": "😔",
    "dubbio": "🤔", "icon_confused": "🤔", "icon_question": "❓",
    "icon_smile_question": "❓", "icon_exclaim": "❗",
    "icon_rolleyes": "🙄", "rolleyes": "🙄",
    "icon_smile_blush": "😳", "icon_redface": "😳", "redface": "😳",
    "icon_smile_shy": "😊", "icon_evil": "😈", "icon_twisted": "😈",
    "icon_smile_evil": "😈", "icon_smile_dead": "💀",
    "icon_smile_approve": "👍", "pollicesu": "👍",
    "icon_smile_dissapprove": "👎", "icon_smile_blackeye": "🤕",
    "icon_smile_sleepy": "😴", "zitto": "🤫", "clown": "🤡",
    "naughty": "😏", "feedtroll": "🧌", "nonono": "🙅", "stordita": "😵",
    "barba": "🧔", "birthday": "🎂", "love": "❤️", "haveniceday": "👋",
    "senzasperanza": "😩", "mmmm": "🤨",
}
# The long tail comes from packs named by theme with a serial number
# (`cibo28.gif`, `sonno39.gif`): the number says nothing, the prefix does.
SMILEY_FAMILY = (
    ("felici", "😄"), ("lingua", "😛"), ("conf", "🤔"), ("cool", "😎"),
    ("sonno", "😴"), ("love", "❤️"), ("angel", "😇"), ("sport", "⚽"),
    ("cibo", "🍽️"), ("jump", "🤸"),
)


def smiley_for(name: str) -> str | None:
    """Emoji for a smiley file name, or None when the pack is unreadable."""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    if stem in SMILEY_EMOJI:
        return SMILEY_EMOJI[stem]
    for prefix, emoji in SMILEY_FAMILY:
        if stem.startswith(prefix) and stem[len(prefix):].isdigit():
            return emoji
    return None


def smiley_span(name: str) -> str:
    """Render one smiley.  The file name is kept as the tooltip: it is the
    only surviving evidence of which image the poster actually picked."""
    emoji = smiley_for(name)
    tip = esc(name.rsplit("/", 1)[-1])
    if emoji:
        return f'<span class="emo" title="{tip}">{emoji}</span>'
    # 5% of the tail (`cart31.gif`, `kaoani09.gif`) is anyone's guess: an
    # invented emoji would be a worse record than an honest placeholder.
    return f'<span class="smiley" title="{tip}">*</span>'


def _smiley_to_text(m: re.Match[str]) -> str:
    tag = m.group(0)
    hit = RE_SRC.search(tag)
    if hit:
        return smiley_span(hit.group(1))
    for rx in (RE_ALT, RE_TITLE):
        hit = rx.search(tag)
        if hit and hit.group(1).strip():
            return f'<span class="smiley">{esc(hit.group(1))}</span>'
    return '<span class="smiley">*</span>'


def sanitise(body: str) -> str:
    body = RE_BAD_BLOCK.sub("", body)
    body = RE_BAD_OPEN.sub("", body)
    body = RE_ON_ATTR.sub("", body)
    body = RE_JS_URL.sub(r"\1#", body)
    return RE_SMILEY.sub(_smiley_to_text, body)


# ------------------------------------------------------------------ entities

# vBulletin escaped its own output on the way into the lo-fi page, so a `&gt;`
# typed by a user reaches us as `&amp;gt;` and renders as literal `&gt;`.  Peel
# those layers off, then decode the entities that cannot become markup — that
# last step matters because plenty of BBCode arrives as `&#91;b&#93;`.
RE_DOUBLE_ENT = re.compile(r"&amp;(#?\w{1,8};)")
RE_ENTITY = re.compile(r"&(#?\w{1,8});")
# Anything that could re-open a tag stays an entity, whatever form it came in.
ENT_KEEP = {"lt", "gt", "amp", "quot", "apos", "#60", "#62", "#38", "#34", "#39"}


def _decode_one(m: re.Match[str]) -> str:
    if m.group(1).lower() in ENT_KEEP:
        return m.group(0)
    ch = html.unescape(m.group(0))
    return m.group(0) if ch == m.group(0) or ch in "<>&\"'" else ch


def unescape_entities(body: str) -> str:
    for _ in range(4):
        peeled = RE_DOUBLE_ENT.sub(r"&\1", body)
        if peeled == body:
            break
        body = peeled
    return RE_ENTITY.sub(_decode_one, body)


# -------------------------------------------------------------------- BBCode

# The lo-fi archive pages never ran the BBCode parser: 4141 posts still carry
# the raw tags.  Only the tags below are interpreted — `[actarus]` and friends
# are nicknames in square brackets, not markup, and must survive as text.
# The author may itself contain brackets — `[quote=[N]e[O]]` is a real nick on
# this forum — so allow balanced `[...]` groups inside it.
RE_BB_QUOTE_WHO = re.compile(
    r"\[quote=\s*((?:[^\[\]\n]|\[[^\[\]\n]{0,30}\]){1,80}?)\s*\](.*?)\[/quote\]",
    re.I | re.S)
RE_BB_QUOTE = re.compile(r"\[quote\](.*?)\[/quote\]", re.I | re.S)
RE_BB_CODE = re.compile(r"\[code\](.*?)\[/code\]", re.I | re.S)
RE_BB_LIST = re.compile(r"\[list(=1)?\](.*?)\[/list\]", re.I | re.S)
RE_BB_ITEM = re.compile(r"\[\*\]")
RE_BB_URL_TXT = re.compile(r"\[url=\s*&quot;?\s*([^\]\s\"']{1,300}?)&?q?u?o?t?;?\s*\](.*?)\[/url\]",
                           re.I | re.S)
RE_BB_URL = re.compile(r"\[url\]\s*([^\[\s]{1,300}?)\s*\[/url\]", re.I | re.S)
RE_BB_IMG = re.compile(r"\[img\]\s*([^\[\s]{1,300}?)\s*\[/img\]", re.I | re.S)
RE_BB_COLOR = re.compile(r"\[color=\s*(\#[0-9a-f]{3,8}|[a-z]{2,20})\s*\](.*?)\[/color\]",
                         re.I | re.S)
RE_BB_SIZE = re.compile(r"\[size=\s*(-?\d{1,2})\s*\](.*?)\[/size\]", re.I | re.S)
RE_BB_FONT = re.compile(r"\[font=\s*([\w \-]{1,40})\s*\](.*?)\[/font\]", re.I | re.S)
RE_BB_SIMPLE = re.compile(r"\[(b|i|u|s|center)\](.*?)\[/\1\]", re.I | re.S)
RE_SAFE_URL = re.compile(r"^(https?|ftp)://[^\s\"'<>]+$", re.I)

BB_SIMPLE = {"b": ("<strong>", "</strong>"), "i": ("<em>", "</em>"),
             "u": ("<u>", "</u>"), "s": ("<s>", "</s>"),
             "center": ('<div class="bbc">', "</div>")}
# vBulletin's size scale is 1-7, not pixels; anything else is left alone.
BB_SIZE = {1: ".7em", 2: ".85em", 3: "1em", 4: "1.15em",
           5: "1.35em", 6: "1.6em", 7: "1.9em"}


def _bb_link(url: str, text: str) -> str:
    if not RE_SAFE_URL.match(url):
        return text
    return (f'<a href="{esc(url)}" rel="nofollow noopener" '
            f'target="_blank">{text}</a>')


def _bb_img(m: re.Match[str]) -> str:
    url = m.group(1)
    if "/smile" in url.lower():          # icon_razz.gif & co: same map as <img>
        return smiley_span(url)
    return _bb_link(url, "[immagine]") or "[immagine]"


def _bb_size(m: re.Match[str]) -> str:
    em = BB_SIZE.get(int(m.group(1)))
    return f'<span style="font-size:{em}">{m.group(2)}</span>' if em else m.group(2)


def _bb_list(m: re.Match[str]) -> str:
    items = [i.strip() for i in RE_BB_ITEM.split(m.group(2))]
    items = [i for i in items if i and i not in ("<br />", "<br>")]
    if not items:
        return ""
    tag = "ol" if m.group(1) else "ul"
    return f'<{tag} class="bbl">' + "".join(f"<li>{i}</li>" for i in items) + f"</{tag}>"


# Openers that can only be markup, so an unclosed one is safe to close at the
# end of the post.  `[b]`, `[i]`, `[s]` are deliberately NOT in here: on this
# forum they are overwhelmingly nicknames — `[S]uicune`, `ALIA[S]{GoNe}`,
# `[s]o[z]e` — and auto-closing them would strike through half a post.
RE_BB_ATTR_OPEN = re.compile(r"\[(quote|color|size|font|code|list)(=[^\]\n]{0,80})?\]", re.I)
RE_BB_ORPHAN_CLOSE = re.compile(
    r"\[/(quote|color|size|font|code|list|url|img|b|i|u|s|center)\]", re.I)


def _balance(body: str) -> str:
    """Close what was left open, then drop the closers that never had an opener."""
    for tag in ("quote", "color", "size", "font", "code", "list"):
        opens = len(re.findall(rf"\[{tag}(=[^\]\n]{{0,80}})?\]", body, re.I))
        closes = len(re.findall(rf"\[/{tag}\]", body, re.I))
        if opens > closes:
            body += f"[/{tag}]" * (opens - closes)
    return body


# phpBB stamped a per-post uid on every tag: `[quote:730d5702f9=&quot;nick&quot;]`.
# Strip the uid and the quoting around the author so the normal rules apply.
RE_BB_UID = re.compile(r"\[(/?)(\w{1,8}):[0-9a-f]{6,12}(=[^\]\n]{0,90})?\]", re.I)
RE_BB_WHO_QUOTED = re.compile(r"^(?:&quot;|&#34;|\")(.*?)(?:&quot;|&#34;|\")$")


def bbcode(body: str) -> str:
    """Render the BBCode the lo-fi pages left raw. Unknown tags stay text."""
    if "[" not in body:
        return body
    body = RE_BB_UID.sub(r"[\1\2\3]", body)
    if RE_BB_ATTR_OPEN.search(body):
        body = _balance(body)
    body = RE_BB_CODE.sub(lambda m: f'<pre class="bbc-code">{m.group(1).strip()}</pre>', body)
    for _ in range(8):                    # quotes nest; keep folding until still
        before = body
        body = RE_BB_QUOTE_WHO.sub(
            lambda m: (f'<blockquote class="bbq"><cite>'
                       f'{esc(RE_BB_WHO_QUOTED.sub(r"\1", m.group(1)))} ha scritto:'
                       f"</cite>{m.group(2)}</blockquote>"), body)
        body = RE_BB_QUOTE.sub(r'<blockquote class="bbq">\1</blockquote>', body)
        body = RE_BB_SIMPLE.sub(
            lambda m: BB_SIMPLE[m.group(1).lower()][0] + m.group(2)
            + BB_SIMPLE[m.group(1).lower()][1], body)
        body = RE_BB_COLOR.sub(r'<span style="color:\1">\2</span>', body)
        body = RE_BB_SIZE.sub(_bb_size, body)
        body = RE_BB_FONT.sub(r'<span style="font-family:\1,serif">\2</span>', body)
        body = RE_BB_LIST.sub(_bb_list, body)
        body = RE_BB_URL_TXT.sub(lambda m: _bb_link(m.group(1), m.group(2)), body)
        body = RE_BB_URL.sub(lambda m: _bb_link(m.group(1), esc(m.group(1))), body)
        body = RE_BB_IMG.sub(_bb_img, body)
        if body == before:
            break
    # Whatever closing tag is still standing never had an opener — the snapshot
    # ate it, or the poster typed it alone. Showing `[/quote]` helps nobody.
    return RE_BB_ORPHAN_CLOSE.sub("", body)


def body_html(raw: str) -> str:
    """Entities first (BBCode hides inside `&#91;b&#93;`), then tags, then scrub."""
    return sanitise(bbcode(unescape_entities(raw)))


RE_BB_ANY = re.compile(
    r"\[/?(?:quote|color|size|font|url|img|code|list|center|b|i|u|s|\*)"
    r"(?::[0-9a-f]{6,12})?(?:=[^\]\n]{0,90})?\]", re.I)
RE_WS = re.compile(r"\s+")


def plain(text: str) -> str:
    """One-line summary for `<meta name=description>`: no entities, no BBCode.

    Unlike `body_html` this decodes `&lt;`/`&gt;` too — the result is a plain
    string that `esc()` re-escapes for the attribute, so `<scorpion4>` must be
    a real character here or it ships as a visible `&lt;scorpion4&gt;`.
    """
    return RE_WS.sub(" ", html.unescape(RE_BB_ANY.sub("", unescape_entities(text or "")))).strip()


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
header.top .site{font-size:1.15rem;font-weight:600;margin:0 0 .2rem}
header.top .crumb{font-size:.85rem;color:var(--dim)}
header.top .find{font-size:.85rem;white-space:nowrap}
h1.tt,h2.tt{font-size:1.35rem;line-height:1.3;margin:.2rem 0 .3rem;font-weight:600}
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
.emo{font-size:1.1em;line-height:1;font-family:"Apple Color Emoji","Segoe UI Emoji",
  "Noto Color Emoji",sans-serif}
.trunc{font-size:.8rem;color:var(--acc);margin-top:.4rem}
blockquote.bbq{margin:.5rem 0;padding:.4rem .7rem;border-left:3px solid var(--line);
background:var(--bg);border-radius:0 4px 4px 0}
blockquote.bbq cite{display:block;font-size:.8rem;color:var(--dim);font-style:normal;
margin-bottom:.25rem}
pre.bbc-code{margin:.5rem 0;padding:.5rem .7rem;background:var(--bg);border:1px solid
var(--line);border-radius:4px;overflow-x:auto;font-size:.85em;white-space:pre-wrap}
ul.bbl,ol.bbl{margin:.4rem 0 .4rem 1.2rem;padding:0}
.bbc{text-align:center}
.pager{margin:1.2rem 0;font-size:.9rem}
.pager a,.pager span{display:inline-block;padding:.15rem .45rem}
.pager .cur{background:var(--line);border-radius:4px}
footer.foot{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:.8rem;
font-size:.8rem;color:var(--dim)}
table{max-width:100%;display:block;overflow-x:auto}
/* Pagefind ships a light-only default palette: feed it our own tokens so the
   search page follows prefers-color-scheme like every other page. */
:root{--pagefind-ui-primary:var(--fg);--pagefind-ui-text:var(--fg);
--pagefind-ui-background:var(--card);--pagefind-ui-border:var(--line);
--pagefind-ui-tag:var(--line);--pagefind-ui-border-width:1px;
--pagefind-ui-border-radius:6px;--pagefind-ui-font:inherit}
.pagefind-ui__search-input,.pagefind-ui__search-clear{background:var(--card);
color:var(--fg);border-color:var(--line)}
.pagefind-ui__search-input::placeholder{color:var(--dim)}
.pagefind-ui__result{border-top:1px solid var(--line)}
.pagefind-ui__result-link{color:var(--acc)}
.pagefind-ui__result-excerpt,.pagefind-ui__message{color:var(--fg)}
.pagefind-ui__result-excerpt mark{background:var(--line);color:var(--fg)}
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
<header class="top" data-pagefind-ignore><div>
<div class="site"><a href="{root}">Archivio forum Azzurra</a></div>
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
                f'<div class="body">{body_html(p["body_html"])}</div>{trunc}</article>'
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
              desc=(plain(posts[0]["body_text"])[:180] if posts else
                    f"Discussione {t['id']} dei forum di Azzurra."),
              body=(f'<div data-pagefind-body>'
                    f'<h1 class="tt" data-pagefind-meta="title">{esc(title)}</h1>'
                    f'{"".join(blocks)}</div>'))
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
