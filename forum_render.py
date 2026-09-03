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
import collections
import hashlib
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
    # The board's own short codes, the ones that also survive as bare text
    # (`:rotfl:`, `:okay:`) where the snapshot dropped the <img> tag.
    "rotfl": "🤣", "okay": "👌", "prrr": "😛", "lol": "😂", "nope": "🙅",
    "smart": "🤓", "grin": "😁", "proud": "😤", "razz": "😛",
    # No `d`/`p`/`o`: `:D:P:O` are ASCII emoticons the poster typed by hand,
    # and a run of them (`:D:D:D`) would be eaten as codes.  They read fine.
    "sad": "🙁", "up": "👍", "down": "👎",
    "groan": "😩", "dead": "💀", "cry": "😢", "sleepy": "😴", "zzz": "😴",
    "shh": "🤫", "cool": "😎", "neutral": "😐", "blackeye": "🤕",
    "confused": "🤔", "noway": "🙅", "sbav": "🤤", "gaah": "😫",
    "evil": "😈", "shy": "😊", "happy": "😄", "roll": "🙄", "perplex": "😕",
    "quest": "❓", "sun": "☀️", "arrow": "➡️", "exclaim": "❗",
}
# The long tail comes from packs named by theme with a serial number
# (`cibo28.gif`, `sonno39.gif`): the number says nothing, the prefix does.
SMILEY_FAMILY = (
    ("felici", "😄"), ("lingua", "😛"), ("conf", "🤔"), ("cool", "😎"),
    ("sonno", "😴"), ("love", "❤️"), ("angel", "😇"), ("sport", "⚽"),
    ("cibo", "🍽️"), ("jump", "🤸"),
    # Same shape, but these prefixes say what the face does, so the serial can
    # be dropped without inventing anything: `shy2.gif`, `eek1.gif`, `mad2.gif`.
    ("shy", "😊"), ("sad", "🙁"), ("eek", "😲"), ("roll", "🙄"),
    ("happy", "😄"), ("perplex", "😕"), ("evil", "😈"), ("mad", "😠"),
    ("wink", "😉"), ("quest", "❓"), ("sun", "☀️"),
)

# Packs whose names carry no meaning at all (`cart31`, `kaoani09`): the image is
# gone and the name says nothing, so they keep the honest `*` placeholder.
UNKNOWN_PACKS = ("cart", "kaoani", "anim", "spec", "donia", "varie", "icone")


def smiley_for(name: str) -> str | None:
    """Emoji for a smiley file name, or None when the pack is unreadable."""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    if stem in SMILEY_EMOJI:
        return SMILEY_EMOJI[stem]
    for prefix, emoji in SMILEY_FAMILY:
        if stem.startswith(prefix) and stem[len(prefix):].isdigit():
            return emoji
    return None


# The GIFs themselves came back: the Archive did capture the board's own
# /images/smilies/ tree (508 files), which an earlier pass had written off.
# So the emoji map is now the FALLBACK, not the answer — `smilies/` is filled
# by fetch_smilies.sh and copied into the site at render time.  Keys are both
# the path under images/smilies/ and the bare stem, because half the posts name
# the file (`classic/icon_smile.gif`) and half only the code (`:rotfl:`).
SMILEY_DIR = Path("smilies")
SMILEY_FILES: dict[str, str] = {}


def load_smilies() -> None:
    SMILEY_FILES.clear()
    if not SMILEY_DIR.is_dir():
        return
    for f in SMILEY_DIR.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(SMILEY_DIR).as_posix()
        SMILEY_FILES[rel.lower()] = rel
        # A bare stem is ambiguous across packs; first one in wins and the
        # packs do not collide in practice (measured: 3 duplicate stems).
        SMILEY_FILES.setdefault(f.stem.lower(), rel)


def smiley_file(name: str) -> str | None:
    """The board's own GIF for this smiley, when the Archive kept it."""
    key = name.split("images/smilies/", 1)[-1].split("images/smiles/", 1)[-1]
    key = key.split("?")[0].lstrip("/").lower()
    return (SMILEY_FILES.get(key)
            or SMILEY_FILES.get(key.rsplit("/", 1)[-1])
            or SMILEY_FILES.get(key.rsplit("/", 1)[-1].rsplit(".", 1)[0]))


def smiley_span(name: str) -> str:
    """Render one smiley.  The file name is kept as the tooltip: it is the
    only surviving evidence of which image the poster actually picked."""
    tip = esc(name.rsplit("/", 1)[-1])
    # Thread pages are the only place post bodies are rendered, and they all
    # sit at thread/<id>-<slug>/, two levels down.
    rel = smiley_file(name)
    if rel:
        return (f'<img class="smi" src="../../smilies/{rel}" alt="{tip}" '
                f'title="{tip}" loading="lazy">')
    emoji = smiley_for(name)
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


# Text codes left naked in the body by the lofi flattening.  Digits alone are
# never a smiley: `:07:` is a timestamp out of a pasted IRC log, and there are
# ~7000 of those.
RE_TEXT_SMILEY = re.compile(r":([a-z][a-z0-9_]{0,14}):", re.I)
RE_TAG_SPLIT = re.compile(r"(<[^>]*>)")


def _text_smiley(m: re.Match[str]) -> str:
    name = m.group(1).lower()
    rel = smiley_file(name)
    if rel:
        return (f'<img class="smi" src="../../smilies/{rel}" alt=":{name}:" '
                f'title=":{name}:" loading="lazy">')
    emoji = smiley_for(name)
    if emoji:
        return f'<span class="emo" title=":{name}:">{emoji}</span>'
    stem = name.rstrip("0123456789")
    if stem != name and stem in UNKNOWN_PACKS:
        return f'<span class="smiley" title=":{name}:">*</span>'
    # Not a smiley this board ever had: `:mypassword:` and `:http:` are text,
    # and guessing an emoji for them would corrupt the post.
    return m.group(0)


def _sub_text_smilies(text: str) -> str:
    """Codes come in runs (`:eek2::sad2:`, `:love::wink:`) where one colon both
    closes a code and opens the next, so the scan backs up one char every time
    instead of consuming the closer."""
    out, i = [], 0
    while True:
        m = RE_TEXT_SMILEY.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        rep = _text_smiley(m)
        out.append(text[i:m.start()])
        if rep == m.group(0):          # not a smiley — put the text back as it was
            out.append(text[m.start():m.end() - 1])
            i = m.end() - 1
        else:
            out.append(rep)
            # Hand the closing colon to the next code only if there is one:
            # otherwise it is this code's own closer and must not survive.
            nxt = m.end() - 1
            i = nxt if RE_TEXT_SMILEY.match(text, nxt) else m.end()


def text_smilies(body: str) -> str:
    """Map the bare `:name:` codes, and only those the smiley map can name."""
    if ":" not in body:
        return body
    return "".join(
        part if part.startswith("<") else _sub_text_smilies(part)
        for part in RE_TAG_SPLIT.split(body)
    )


def sanitise(body: str) -> str:
    body = RE_BAD_BLOCK.sub("", body)
    body = RE_BAD_OPEN.sub("", body)
    body = RE_ON_ATTR.sub("", body)
    body = RE_JS_URL.sub(r"\1#", body)
    return text_smilies(RE_SMILEY.sub(_smiley_to_text, body))


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
    # Only 8 posts in the whole board use [img], and the host is dead in all of
    # them — so this stays a link unless we actually recovered the file, in
    # which case `local_assets()` rewrites the src to our copy.
    if ASSET_FILES and RE_SAFE_URL.match(url) and asset_name(url) in ASSET_FILES:
        return f'<img src="{esc(url)}" alt="" loading="lazy">'
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


# ------------------------------------------------------- flattened quotes
#
# Two quote styles never reached us as BBCode at all: the lo-fi renderer had
# already flattened them to plain text, so `[quote]` is gone and only a header
# line survives.  They are invisible to `bbcode()` above.
#
#   vB2 (2001-2003):  `In data 2002-03-30 17:34, overruns scrive:`
#   vB3 (2004-2016):  `Citazione:` + `Originale inviato da <nick>`
#
# Census over the 8833 snapshot pages: 338 vB2 headers in 104 files, 154 vB3
# headers in 55 files (`[quote=...]`, which DOES survive, is 4001 in 410).
#
# The hard part is not the header, it is where the quote ENDS.  vB2 quoting is
# interleaved — quoted chunk, reply, quoted chunk, reply — and the flattening
# left nothing but blank lines between them, so the structure alone cannot say
# which chunk is whose.  What CAN say it: the quoted text is a verbatim copy of
# another post in the same thread.  So each blank-line-separated chunk is
# matched against the rest of the thread; a chunk that appears there verbatim
# is the quote, everything else is the reply.  No heuristic, an equality test.
RE_CHUNK_SPLIT = re.compile(r"(?:\s*<br\s*/?>\s*){2,}", re.I)
RE_VB2_HEAD = re.compile(
    r"^\s*In data\s+([^,<]{4,40}?)\s*,\s*(.{1,40}?)\s+scrive\s*:\s*(?:<br\s*/?>\s*)?",
    re.I)
RE_VB3_HEAD = re.compile(
    r"^\s*Citazione\s*:\s*(?:<br\s*/?>\s*)?"
    r"(?:Originale inviato da\s+(.{1,40}?)\s*(?:<br\s*/?>\s*|$))?",
    re.I)
RE_TAGS = re.compile(r"<[^>]{1,300}>")
# Cheap unanchored probe: is it worth building this thread's sibling text at all?
RE_FLAT_HEAD = re.compile(r"In data\s[^,<]{4,40},.{1,40}?\sscrive\s*:|Citazione\s*:", re.I)
# A chunk this short can collide with an unrelated post by accident ("ok", "si
# quoto"), so below the floor the verbatim test is not evidence of anything.
QUOTE_MIN_CHARS = 25


def norm_text(s: str) -> str:
    """Tag-free, whitespace-collapsed, case-folded — for the verbatim test."""
    return RE_WS.sub(" ", html.unescape(RE_TAGS.sub(" ", s))).strip().lower()


def _wrap(chunks: list[str], cite: str | None) -> str:
    inner = "<br />\n<br />\n".join(chunks)
    head = f"<cite>{esc(cite)} ha scritto:</cite>" if cite else ""
    return f'<blockquote class="bbq">{head}{inner}</blockquote>'


def flat_quotes(body: str, siblings: str) -> str:
    """Rebuild the quote blocks the lo-fi renderer flattened into plain text."""
    chunks = RE_CHUNK_SPLIT.split(body)
    who, seen_head = None, False
    kept, is_quote = [], []
    for chunk in chunks:
        m = RE_VB2_HEAD.match(chunk) or RE_VB3_HEAD.match(chunk)
        if m:
            # A header eats itself; its own chunk's remainder is the quote it
            # introduces, by construction. Only the FIRST header names a nick —
            # later ones in the same post are the same conversation.
            if who is None:
                who = m.group(2) if m.re is RE_VB2_HEAD else m.group(1)
            seen_head = True
            kept.append(chunk[m.end():])
            is_quote.append(True)
            continue
        kept.append(chunk)
        # Everything after a header has to earn the quote: it counts only if it
        # is a verbatim copy of another post in this thread.
        n = norm_text(chunk)
        is_quote.append(seen_head and len(n) >= QUOTE_MIN_CHARS and n in siblings)
    if not seen_head:
        return body

    out, run, first = [], [], True
    for chunk, is_q in zip(kept, is_quote):
        if is_q:
            run.append(chunk)
            continue
        if run:
            out.append(_wrap(run, who if first else None))
            run, first = [], False
        out.append(chunk)
    if run:
        out.append(_wrap(run, who if first else None))
    return "<br />\n<br />\n".join(o for o in out if o.strip())


# ------------------------------------------------------------- IRC log paste
#
# Half this forum is people pasting IRC. Proportional type destroys the column
# the log is read by, so a run of consecutive log-shaped lines becomes a <pre>.
# The nick brackets survive escaped (`&lt;nick&gt;`), which is why this pass
# runs on the sanitised HTML and not before it.
NICK = r"[\w`\[\]{}|^\\-]{1,30}"
RE_LOG_LINE = re.compile(
    # A coloured paste opens the line with the raw control bytes: they are part
    # of the shape too, not a reason to miss the line.
    r"^[\s\x02\x03\x0f\x16\x1d\x1f]*(?:\d{1,2}(?:,\d{1,2})?)?\s*(?:"
    rf"&lt;\s*[@+%~&amp;]?{NICK}\s*&gt;\s"          # <nick> said something
    rf"|\*\s+{NICK}\s"                              # * nick does something
    # 12:34 / [12:34:56] / `[ 13:47:05 ]` — mIRC and irssi both pad inside the
    # brackets, so the spaces are part of the shape, not noise to trim first.
    r"|\[?\s*\d{1,2}:\d{2}(?::\d{2})?\s*\]?\s"
    r"|(?:\*\*\*|--&gt;|&lt;--|-!-|===)\s"          # join/part/mode chatter
    r")", re.I)
RE_BR_SPLIT = re.compile(r"<br\s*/?>", re.I)
LOG_MIN_LINES = 3


def irc_logs(body: str) -> str:
    """Wrap runs of >= 3 consecutive IRC-log lines in a monospace <pre>."""
    if "&lt;" not in body and ":" not in body:
        return body
    lines = RE_BR_SPLIT.split(body)
    out, run = [], []

    def flush() -> None:
        if len(run) >= LOG_MIN_LINES:
            out.append('<pre class="irclog">' + "\n".join(x.strip() for x in run)
                       + "</pre>")
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        if RE_LOG_LINE.match(line):
            run.append(line)
            continue
        # A blank line inside a paste is part of the paste, not the end of it.
        if run and not line.strip():
            run.append(line)
            continue
        flush()
        out.append(line)
    flush()
    # A <pre> is already a block: putting a <br /> against it would open a gap
    # the paste never had.
    parts = []
    for o in out:
        if parts and not (o.startswith("<pre class=") or parts[-1].endswith("</pre>")):
            parts.append("<br />")
        parts.append(o)
    return "".join(parts)


# ------------------------------------------------------------ internal links
# Posts quote each other by URL on the dead board.  Those that name a thread we
# hold get pointed at the local page instead of at a 404.
#
# `showthread.php?t=N`: measured against the post dates, 202 of its 213
# resolvable targets are older than the post linking them (95%, i.e. right).
#
# `viewtopic.php` used to be left alone here, on the grounds that the phpBB
# board numbered its topics in a space we did not hold.  We hold it now: the
# mirror of the old board is merged into the same corpus and every thread it
# stitched carries its `old_topic_id`, every post its `old_post_id`.  Those ids
# resolve locally — 31 of the 105 phpBB topic ids the posts link, plus the
# sections behind `viewforum.php?f=`.  The rest never made it into the mirror
# and still go to the Archive.
#
# The 5% that point *forward* in time used to be dropped as foreign ids too.
# They are not: all 13 of them sit in index posts that were edited for years
# after they were written — the first post of `Il Manabile di #altrove` (2004)
# links its own Appendices A to D (2004 to 2007), and thread 6306 is a link
# list.  `posted_at` is when the post was created, never when it last changed,
# so it cannot rule any link out.  The id resolving inside our own vB numbering
# is the whole test.
#
# vB wrote its own board links relative (`href="showthread.php?s=<sid>&t=N"`),
# and those need the same treatment: on the mirror they resolve against the
# thread directory and 404.  Bare URLs in the text stay absolute-only — a naked
# `showthread.php` in prose is prose.
#
# The board answered on two hostnames over its life: `azzurra.org/forum/` in
# 2004-2005, `forum.azzurra.org/` after the move.  Posts from either era link
# the shape of their own day, and the ids behind both are the ids we hold — 22
# section links and 16 thread links were going to the Archive for a page two
# directories away.
BOARD_HOST = r"(?:www\.)?(?:forum\.azzurra\.org|azzurra\.org/forum)/"
BOARD_Q = r"(?P<q>[\w=&;%#.+-]{0,120})"
RE_OLD_THREAD = re.compile(
    rf"https?://{BOARD_HOST}showthread\.php\?{BOARD_Q}", re.I)
RE_HREF_BOARD = re.compile(
    rf"(?:https?://{BOARD_HOST})?"
    rf"(?P<script>show(?:thread|post)|forumdisplay|view(?:topic|forum))"
    rf"\.php\?{BOARD_Q}", re.I)
RE_VB_SID = re.compile(r"(?:^|&amp;|[&;])s=[0-9a-f]{16,40}", re.I)
RE_Q_T = re.compile(r"(?:^|[&;])t=(\d+)", re.I)
RE_Q_P = re.compile(r"(?:^|[&;])p=(\d+)", re.I)
RE_Q_F = re.compile(r"(?:^|[&;])f=(\d+)", re.I)
# phpBB spelled the same three ids out in full.  `topic=`/`forum=` are the
# 1.4.0 shape, `t=`/`p=`/`f=` the 2.0 one; both appear in the posts.
RE_Q_TOPIC = re.compile(r"(?:^|[&;])topic=(\d+)", re.I)
RE_Q_FORUM = re.compile(r"(?:^|[&;])forum=(\d+)", re.I)
RE_OLD_POST = re.compile(
    rf"https?://{BOARD_HOST}showpost\.php\?{BOARD_Q}", re.I)
# A board section: `forumdisplay.php?f=42` is the "Italia Area" listing, and we
# hold that page ourselves.
RE_OLD_FORUM = re.compile(
    rf"https?://{BOARD_HOST}(?P<script>forumdisplay)\.php\?{BOARD_Q}", re.I)
# The phpBB pair, same treatment.
RE_OLD_VIEW = re.compile(
    rf"https?://{BOARD_HOST}(?P<script>view(?:topic|forum))\.php\?{BOARD_Q}",
    re.I)

THREAD_LINKS: dict[int, tuple[str, str, str]] = {}   # id -> (href, title, first)
POST_LINKS: dict[int, tuple[int, int]] = {}          # vb post id -> (thread, seq)
FORUM_LINKS: dict[int, tuple[str, str]] = {}         # id -> (href, name)
# The same three, keyed by the ids the *phpBB* board used before the migration.
OLD_THREADS: dict[int, int] = {}                     # phpBB topic  -> thread id
OLD_POSTS: dict[int, tuple[int, int]] = {}           # phpBB post   -> (thread, seq)
OLD_FORUMS: dict[int, int] = {}                      # phpBB forum  -> forum id


def _local_href(m: re.Match[str]) -> tuple[str, str] | None:
    """Resolve a board URL to `(href, label)` on the mirror, or None.

    `f=` is read only off a `forumdisplay` URL: vB puts the forum id in plenty
    of thread links too, and a thread link that falls back to its section would
    quietly send the reader to the wrong page.
    """
    q = html.unescape(m.group("q")).replace("&amp;", "&")
    script = (m.groupdict().get("script") or "showthread").lower()
    if script in ("viewtopic", "viewforum"):
        return _old_board_href(script, q)
    if script == "forumdisplay":
        hit = RE_Q_F.search(q)
        fid = int(hit.group(1)) if hit else None
        if fid is None or fid not in FORUM_LINKS:
            return None
        return FORUM_LINKS[fid]
    tid = seq = None
    hit = RE_Q_T.search(q)
    if hit:
        tid = int(hit.group(1))
    else:
        hit = RE_Q_P.search(q)
        if hit and int(hit.group(1)) in POST_LINKS:
            tid, seq = POST_LINKS[int(hit.group(1))]
    if tid is None or tid not in THREAD_LINKS:
        return None
    href, title, _first = THREAD_LINKS[tid]
    return href + (f"#post-{seq}" if seq else ""), title


def _old_board_href(script: str, q: str) -> tuple[str, str] | None:
    """The phpBB side of the same job: `viewtopic.php` / `viewforum.php`.

    A topic id is worth more than a post id here — `p=` was only ever written by
    the 2.0 skin and the mirror holds the page, not the anchor, for most of them
    — so `t=`/`topic=` is tried first and `p=` only fills in the gap.
    """
    if script == "viewforum":
        hit = RE_Q_F.search(q) or RE_Q_FORUM.search(q)
        fid = OLD_FORUMS.get(int(hit.group(1))) if hit else None
        return FORUM_LINKS.get(fid) if fid is not None else None
    seq = None
    hit = RE_Q_T.search(q) or RE_Q_TOPIC.search(q)
    tid = OLD_THREADS.get(int(hit.group(1))) if hit else None
    if tid is None:
        hit = RE_Q_P.search(q)
        if hit and int(hit.group(1)) in OLD_POSTS:
            tid, seq = OLD_POSTS[int(hit.group(1))]
    if tid is None or tid not in THREAD_LINKS:
        return None
    href, title, _first = THREAD_LINKS[tid]
    return href + (f"#post-{seq}" if seq else ""), title


RE_A_HREF = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)
RE_A_BLOCK = re.compile(r"<a\s[^>]*>.*?</a>", re.I | re.S)


def _outside_anchors(body: str, fn) -> str:
    """Apply `fn` to the text between anchors, never inside one.

    A post that linked the board relatively and showed the absolute URL as the
    link text — vB's own habit — used to come out as `<a …><a …>title</a></a>`:
    the bare-URL pass rewrote the visible text of a link it had already left
    alone.  Nested anchors are not markup any browser agrees on.
    """
    out, pos = [], 0
    for m in RE_A_BLOCK.finditer(body):
        out.append(fn(body[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(body[pos:]))
    return "".join(out)


def internal_links(body: str, when_posted: str = "") -> str:
    """Repoint old board URLs at the local pages, in the href and in the text."""
    if not any(s in body for s in
               ("showthread.php", "showpost.php", "forumdisplay.php",
                "viewtopic.php", "viewforum.php")):
        return body

    def fix_anchor(m: re.Match[str]) -> str:
        url, text = m.group(1), m.group(2)
        hit = RE_HREF_BOARD.match(url)
        found = _local_href(hit) if hit else None
        if not found:
            # A relative board link we cannot resolve (a `p=` id off a page the
            # Archive never took) points at nothing here.  Spell it out as the
            # board URL it was, minus vB's session id, and let `archive_links`
            # send it where the copy actually is.
            if hit and not url.lower().startswith("http"):
                q = RE_VB_SID.sub("", hit.group("q")).lstrip("&;")
                # phpBB answered on the other hostname: a relative `viewtopic`
                # spelled out as `forum.azzurra.org/` would send the Archive
                # after a page that never lived there.
                base = ("http://www.azzurra.org/forum/"
                        if hit.group("script").lower().startswith("view")
                        else "http://forum.azzurra.org/")
                return m.group(0).replace(
                    f'"{url}"', f'"{base}{url.split("?")[0]}?{q}"')
            return m.group(0)
        new, label = found
        # When the visible text is the dead URL itself, the thread title (or the
        # section name) says more than a link nobody can follow.
        plain = RE_TAGS.sub("", text)
        if "azzurra.org" in plain or "showthread" in text or "forumdisplay" in text:
            text = esc(label)
        return f'<a href="{new}">{text}</a>'

    body = RE_A_HREF.sub(fix_anchor, body)

    def fix_bare(m: re.Match[str]) -> str:
        found = _local_href(m)
        if not found:
            return m.group(0)
        new, label = found
        return f'<a href="{new}">{esc(label)}</a>'

    return _outside_anchors(
        body,
        lambda chunk: RE_OLD_VIEW.sub(
            fix_bare, RE_OLD_FORUM.sub(
                fix_bare, RE_OLD_POST.sub(
                    fix_bare, RE_OLD_THREAD.sub(fix_bare, chunk)))))


# Whatever is left points at a board that has been down since 2016: the phpBB
# ids (`viewtopic.php?topic=693&forum=25`) belong to a numbering we do not hold,
# the member and reply URLs never had a local page at all.  The Archive is the
# only place those still resolve, so they go there, dated to the post that
# links them.
RE_OLD_ANY = re.compile(
    r"https?://(?:www\.)?(?:forum\.)?azzurra\.org/[^\s\"'<>\]\[)]*", re.I)
RE_HREF_ATTR = re.compile(r"(href=\")([^\"]+)(\")", re.I)
WAYBACK = "https://web.archive.org/web/{year}/{url}"


def _wayback(url: str, year: str) -> str:
    # The href is HTML: the query string's `&` has to go back in escaped.
    plain = html.unescape(url)
    return WAYBACK.format(year=year, url=plain).replace("&", "&amp;")


def flatten_anchors(body: str) -> str:
    """Last net: an anchor inside an anchor keeps its text, loses its tags.

    The link passes cannot nest any more, but the 2005 posters could — one post
    ships `[URL=x]<a …>[/URL` with the closing bracket missing, and BBCode does
    what it is told.  Browsers disagree on what that means; nobody disagrees
    about the text.
    """
    if body.count("<a ") < 2:
        return body
    out: list[str] = []
    open_a = False
    dropped: list[bool] = []
    for part in RE_TAG_SPLIT.split(body):
        head = part[:3].lower()
        if part.startswith("<") and head.startswith("<a") and not head.startswith("</"):
            dropped.append(open_a)
            if open_a:
                continue
            open_a = True
        elif part.startswith("<") and head.startswith("</a"):
            if dropped and dropped.pop():
                continue
            open_a = False
        out.append(part)
    return "".join(out)


def archive_links(body: str, when_posted: str = "") -> str:
    """Send the old board URLs we cannot serve to the Archive's copy."""
    if "azzurra.org" not in body:
        return body
    year = (when_posted[:4] if when_posted[:4].isdigit() else "2005")

    def in_tag(tag: str) -> str:
        if "href=" not in tag.lower():
            return tag                      # <img src=...> is left alone
        return RE_HREF_ATTR.sub(
            lambda m: m.group(1) + (_wayback(m.group(2), year)
                                    if RE_OLD_ANY.fullmatch(m.group(2))
                                    else m.group(2)) + m.group(3), tag)

    def in_text(m: re.Match[str]) -> str:
        # The URL stays visible exactly as it was typed: it is the record. Only
        # the destination changes, and it becomes clickable on the way.
        return (f'<a href="{_wayback(m.group(0), year)}" '
                f'title="copia su archive.org">{m.group(0)}</a>')

    # A board URL used as the *text* of a link is already inside an `<a>`: wrap
    # it again and the post ships nested anchors.  Depth counting is enough —
    # `sanitise` has already thrown out whatever else claimed to be markup.
    out, depth = [], 0
    for part in RE_TAG_SPLIT.split(body):
        if part.startswith("<"):
            low = part[:3].lower()
            if low.startswith("<a") and not part.startswith("</"):
                depth += 1
            elif low.startswith("</a"):
                depth = max(0, depth - 1)
            out.append(in_tag(part))
        else:
            out.append(part if depth else RE_OLD_ANY.sub(in_text, part))
    return "".join(out)


# ------------------------------------------------------------ hotlinked imgs
# The posters hotlinked from ~200 hosts, nearly all dead for fifteen years.
# fetch_assets.sh pulls back whatever the Archive kept, named by the sha1 of
# the URL plus its extension — the basenames collide constantly (twenty
# different `image.jpg`) and the hash is the only thing that does not.  The
# ones the Archive never took keep the original `src`: a broken image that
# still names the host it came from is a better record than a silent deletion.
ASSET_DIR = Path("assets")
SKIN_DIR = Path("skin")
ASSET_FILES: set[str] = set()

RE_IMG_SRC_ATTR = re.compile(r"(<img[^>]*\ssrc=\")([^\"]+)(\")", re.I)


def load_assets() -> None:
    ASSET_FILES.clear()
    if ASSET_DIR.is_dir():
        ASSET_FILES.update(
            f.name for f in ASSET_DIR.iterdir()
            if f.is_file() and f.stat().st_size and not f.name.endswith(".part")
        )


def asset_name(url: str) -> str:
    """The name fetch_assets.sh wrote — must stay in step with assets_list.py."""
    ext = re.sub(r"[^a-z0-9]", "", url.split("?")[0].rsplit(".", 1)[-1].lower())[:4]
    return hashlib.sha1(url.encode()).hexdigest() + "." + (ext or "bin")


def local_assets(body: str) -> str:
    """Point the surviving hotlinks at our own copy."""
    if not ASSET_FILES or "<img" not in body:
        return body

    def one(m: re.Match[str]) -> str:
        # assets_list.py hashed the URL with only `&amp;` undone, so undo
        # exactly that much here or the two names never meet.
        url = m.group(2).strip().replace("&amp;", "&")
        if not url.lower().startswith(("http://", "https://")):
            return m.group(0)
        name = asset_name(url)
        if name not in ASSET_FILES:
            return m.group(0)
        # Thread pages sit two levels down, same as the smilies above.
        return f"{m.group(1)}../../assets/{name}{m.group(3)}"

    return RE_IMG_SRC_ATTR.sub(one, body)


# ------------------------------------------------------------- mIRC controls
# Pasted logs carry the raw control bytes the client wrote: 0x03 colour (with
# `fg[,bg]` in decimal), 0x02 bold, 0x1f underline, 0x1d italic, 0x16 reverse,
# 0x0f reset.  118 posts have them, 5662 colour codes in all.  Rendering them
# is the whole point of pasting a coloured log.
MIRC_PALETTE = (
    "#ffffff", "#000000", "#00007f", "#009300", "#ff0000", "#7f0000",
    "#9c009c", "#fc7f00", "#ffff00", "#00fc00", "#009393", "#00ffff",
    "#0000fc", "#ff00ff", "#7f7f7f", "#d2d2d2",
)
RE_MIRC_COLOR = re.compile(r"\x03(\d{1,2})?(?:,(\d{1,2}))?")
MIRC_CTRL = "\x02\x03\x0f\x16\x1d\x1f"


def _mirc_style(fg: int | None, bg: int | None, bold: bool,
                under: bool, italic: bool, rev: bool) -> str:
    if rev:
        fg, bg = (bg if bg is not None else 0), (fg if fg is not None else 1)
    css = []
    if fg is not None:
        css.append(f"color:{MIRC_PALETTE[fg % 16]}")
        # mIRC drew on a white window: black-on-black is not what the poster
        # saw, so the dark half of the palette brings that canvas with it.
        if bg is None and fg % 16 in (1, 2, 5, 6, 12, 14):
            bg = 0
    if bg is not None:
        css.append(f"background:{MIRC_PALETTE[bg % 16]}")
    if bold:
        css.append("font-weight:700")
    if under:
        css.append("text-decoration:underline")
    if italic:
        css.append("font-style:italic")
    return ";".join(css)


def _mirc_segment(text: str) -> str:
    """One text run: close and reopen a single span at every state change, so
    the markup stays balanced whatever the paste does."""
    out, i = [], 0
    fg = bg = None
    bold = under = italic = rev = False
    open_span = False

    def restyle() -> None:
        """Close what is open; the next span opens only when text needs it, so
        a run of codes with nothing between them leaves no empty markup."""
        nonlocal open_span
        if open_span:
            out.append("</span>")
            open_span = False

    def emit(ch: str) -> None:
        nonlocal open_span
        if not open_span:
            css = _mirc_style(fg, bg, bold, under, italic, rev)
            if css:
                out.append(f'<span style="{css}">')
                open_span = True
        out.append(ch)

    while i < len(text):
        ch = text[i]
        if ch == "\x03":
            m = RE_MIRC_COLOR.match(text, i)
            fg = int(m.group(1)) if m.group(1) else None
            bg = int(m.group(2)) if m.group(2) else (bg if m.group(1) else None)
            i = m.end()
            restyle()
            continue
        if ch in MIRC_CTRL:
            if ch == "\x02":
                bold = not bold
            elif ch == "\x1f":
                under = not under
            elif ch == "\x1d":
                italic = not italic
            elif ch == "\x16":
                rev = not rev
            else:                                   # 0x0f — plain text again
                fg = bg = None
                bold = under = italic = rev = False
            i += 1
            restyle()
            continue
        if ch == "\n":                              # mIRC state dies with the line
            restyle()
            fg = bg = None
            bold = under = italic = rev = False
            out.append(ch)
            i += 1
            continue
        emit(ch)
        i += 1
    if open_span:
        out.append("</span>")
    return "".join(out)


def mirc_colors(body: str) -> str:
    """Turn the mIRC control bytes into spans, leaving the tags untouched."""
    if not any(c in body for c in MIRC_CTRL):
        return body
    return "".join(
        part if part.startswith("<") else _mirc_segment(part)
        for part in RE_TAG_SPLIT.split(body)
    )


# vB2 wrote its own edit footer escaped — `&lt;font size=-1&gt;[ Questo
# messaggio e' stato modificato da: ... ]&lt;/font&gt;` — so the board printed
# the tags at the reader instead of the note.  456 posts carry it.  The other
# escaped <font>s (29) are the posters' own markup, escaped by the same bug.
RE_EDIT_NOTE = re.compile(
    r"&lt;\s*font[^&]{0,60}?&gt;\s*\[\s*(Questo messaggio[^\]]{0,200}?)\s*\]\s*"
    r"&lt;\s*/\s*font\s*&gt;", re.I)
RE_ESC_FONT = re.compile(r"&lt;\s*(/?\s*font[^&]{0,60}?)\s*&gt;", re.I)


def edit_notes(body: str) -> str:
    if "&lt;" not in body:
        return body
    body = RE_EDIT_NOTE.sub(lambda m: f'<div class="edited">[{m.group(1)}]</div>',
                            body)
    return RE_ESC_FONT.sub(lambda m: f"<{m.group(1)}>", body)


# --------------------------------------------------- vBulletin quote/code boxes
#
# The full `showthread` pages never went through BBCode: vB had already rendered
# quotes and code as markup, and until the import learned to count nested divs
# those boxes were cut off the end of 2780 posts.  Now they arrive intact, as a
# table (quote) or a `<pre class="alt2">` (code) wrapped in a labelled div:
#
#   <div ...><div class="smallfont">Cita:</div><table>…<td class="alt2">BODY</td>…</table></div>
#   <div ...><div class="smallfont">Codice:</div><pre class="alt2" …>BODY</pre></div>
#
# Reuse the site's own `blockquote.bbq` / `pre.bbc-code` instead of shipping the
# 2004 table markup: same shape as every other quote on the board.  The `Cita:`
# label is vB's chrome, but the header INSIDE the cell names the author, so it
# becomes the `<cite>` — and lifting it out here also keeps `flat_quotes()` from
# meeting a header it would wrap a second time.
RE_VB_BOX = re.compile(
    r'<div[^>]*>\s*<div class="smallfont"[^>]*>\s*'
    r'(?P<kind>Cita|Citazione|Quote|Codice|Code)\s*:\s*</div>\s*', re.I)
RE_VB_CELL = re.compile(r'<td[^>]*class="alt2"[^>]*>', re.I)
RE_VB_PRE = re.compile(r'<pre[^>]*>(?P<in>.*?)</pre>', re.I | re.S)
# `Originale inviato da` (vB3) and `Scritto originariamente da` (the later skin,
# which also wraps the line in its own `<div>`) are the same header.
RE_VB3_CITE = re.compile(
    r'^\s*(?:<br\s*/?>\s*)*(?:<div[^>]*>\s*)?(?:Citazione\s*:\s*(?:<br\s*/?>\s*)*)?'
    r'(?:Originale inviato da|Scritto originariamente da)\s*'
    r'<(?:b|strong)>(?P<who>.{1,40}?)</(?:b|strong)>'
    r'\s*(?:</div>\s*)?(?:<br\s*/?>\s*)*', re.I)
RE_VB2_CITE = re.compile(
    r'^\s*(?:<br\s*/?>\s*)*In data\s+[^,<]{4,40},\s*(?P<who>.{1,40}?)\s+scrive\s*:'
    r'\s*(?:<br\s*/?>\s*)*', re.I)
RE_NEST = {t: re.compile(rf"<(/?){t}\b", re.I) for t in ("div", "td")}


def _tag_end(body: str, start: int, tag: str = "div") -> int:
    """Offset of the `</tag>` closing the one open at depth 1 from `start`, or len.
    Nesting has to be counted: quotes contain quotes, so the FIRST `</td>` after a
    quote cell is usually the inner quote's, not this one's."""
    depth = 1
    for m in RE_NEST[tag].finditer(body, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.start()
    return len(body)


def vb_boxes(body: str, depth: int = 0) -> str:
    # Quotes nest: vB puts the whole inner box inside the outer one's cell, so the
    # cell text has to go through this again or 276 nested boxes keep their tables.
    if "smallfont" not in body or depth > 8:
        return body
    out, pos = [], 0
    while (m := RE_VB_BOX.search(body, pos)) is not None:
        end = _tag_end(body, m.end())
        inner = body[m.end():end]
        code = m.group("kind").lower() in ("codice", "code")
        cell = (RE_VB_PRE if code else RE_VB_CELL).search(inner)
        if cell is None:                  # not the shape we know — leave it alone
            out.append(body[pos:m.end()])
            pos = m.end()
            continue
        if code:
            text = cell.group("in")
            box = f'<pre class="bbc-code">{text.strip()}</pre>'
        else:
            text = vb_boxes(inner[cell.end():_tag_end(inner, cell.end(), "td")],
                            depth + 1)
            cm = RE_VB3_CITE.match(text) or RE_VB2_CITE.match(text)
            cite = f'<cite>{esc(cm.group("who"))} ha scritto:</cite>' if cm else ""
            if cm:
                text = text[cm.end():]
            box = f'<blockquote class="bbq">{cite}{text.strip()}</blockquote>'
        out.append(body[pos:m.start()])
        out.append(box)
        pos = min(end + len("</div>"), len(body))
    out.append(body[pos:])
    return "".join(out)


def body_html(raw: str, siblings: str = "", when_posted: str = "") -> str:
    """Entities first (BBCode hides inside `&#91;b&#93;`), then tags, then scrub."""
    body = edit_notes(bbcode(vb_boxes(unescape_entities(raw))))
    if siblings:
        body = flat_quotes(body, siblings)
    body = internal_links(sanitise(body), when_posted)
    # Colours last: irc_logs still has to see the line starts as text, and the
    # spans this emits would hide them.
    return mirc_colors(irc_logs(local_assets(
        flatten_anchors(archive_links(body, when_posted)))))


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


# The palette is not invented: it is the board's own `Azzurra3.0` skin, vBulletin
# 3.8.2, style id 6.  Every snapshot carries that stylesheet inline in a
# `<style id="vbulletin_css">` block, so the colours below were read off the
# archive rather than guessed — `#11518F` links going `#FF4400` on hover,
# `#97d2ec` category bars, the `#F3F3FF`/`#FDFDFD` alternating post rows.  The
# four images the skin points at (`fetch_skin.sh`) come from the Archive.
#
# What is NOT copied is the 2009 layout itself: fixed-width tables, nested
# `<td class="alt1">`, a login box in the header.  The mirror has to read on a
# phone and it has no login, so the look is rebuilt with the old paint on top of
# the layout that already worked.  Dark mode is ours too — the skin had none,
# and the hues are pulled towards the same blue rather than replaced.
CSS = """\
:root{--bg:#efefef;--fg:#000;--dim:#555;--line:#ccc;--acc:#11518F;--hot:#f40;
--card:#fff;--cat:#9ad4ec;--catfg:#11518F;--bar:#4f7488;--barfg:#f0f0f0;
--alt1:#f3f3ff;--alt2:#fdfdfd;--catimg:url(skin/sfondotb.png);
--barimg:url(skin/misc/cat_back.png)}
@media(prefers-color-scheme:dark){:root{--bg:#12161a;--fg:#e2e6ea;--dim:#93a1ab;
--line:#2b343c;--acc:#7fb6e6;--hot:#ff7a4d;--card:#171d23;--cat:#1e2c38;
--catfg:#9fd0ee;--bar:#22303b;--barfg:#cfe3ef;--alt1:#171e26;--alt2:#141a20;
--catimg:none;--barimg:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 "Lucida Grande","Lucida Sans Unicode",Verdana,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover,a:active{color:var(--hot)}
.wrap{max-width:62rem;margin:0 auto;padding:.6rem .7rem 3rem}
/* The header is the board's: logo left on its own `#efefef`, then the grey
   breadcrumb strip vB drew as a one-pixel-spaced table. */
header.top{margin:0 0 1rem}
header.top .brand{display:block;line-height:0;margin:0 0 .6rem}
header.top .brand img{max-width:100%;height:auto}
/* The logo is opaque `#efefef`, the light page's own background: on a dark page
   it would be a white slab, so it is dimmed rather than replaced — it is the
   board's banner and there is no dark version of it to use. */
@media(prefers-color-scheme:dark){header.top .brand{background:#e6e6e6;
border:1px solid var(--line)}header.top .brand img{filter:brightness(.88)}}
header.top .site{display:none}
header.top .bar{background:var(--card);border:1px solid var(--line);
padding:.45rem .6rem;display:flex;justify-content:space-between;
align-items:center;gap:1rem;flex-wrap:wrap}
header.top .crumb{font-size:12.5px;color:var(--dim);background:
var(--nav,transparent) no-repeat left center;padding-left:22px;
background-image:url(skin/misc/navbits_start.gif)}
header.top .crumb a{font-weight:bold}
header.top .find{font-size:12.5px;white-space:nowrap}
h1.tt,h2.tt{font-size:1.35rem;line-height:1.3;margin:0 0 .5rem;font-weight:bold;
background:var(--cat) var(--catimg) repeat-x top left;color:var(--catfg);
border:1px solid var(--line);padding:.4rem .6rem}
p.meta{font-size:12.5px;color:var(--dim)}
ul.list{list-style:none;margin:0 0 1rem;padding:0;border:1px solid var(--line);
border-top:0;background:var(--card)}
ul.list li{border-top:1px solid var(--line);padding:.5rem .6rem .5rem 2rem;
background-color:var(--alt2);background-image:url(skin/statusicon/post_old.gif);
background-repeat:no-repeat;background-position:.55rem .7rem}
ul.list li:nth-child(odd){background-color:var(--alt1)}
ul.list li>a{font-weight:bold}
ul.list .meta{font-size:12.5px;color:var(--dim)}
article.post{background:var(--alt2);border:1px solid var(--line);
margin:0 0 .6rem;padding:0;overflow-wrap:anywhere}
article.post:nth-of-type(odd){background:var(--alt1)}
article.post header{font-size:12.5px;color:var(--dim);background:var(--bg);
border-bottom:1px solid var(--line);padding:.35rem .6rem;margin:0}
article.post header .who{color:var(--fg);font-weight:bold;font-size:14px}
article.post .body{overflow-x:auto;padding:.6rem}
article.post>.trunc{padding:0 .6rem .5rem}
article.post img{max-width:100%;height:auto}
.smiley{color:var(--dim);font-size:.85em}
.smi{height:1.3em;width:auto;vertical-align:-.25em;display:inline-block}
#q{width:100%;padding:.6rem .7rem;font:inherit;color:var(--fg);
background:var(--card);border:1px solid var(--line);border-radius:6px}
.res{list-style:none;padding:0;margin:1rem 0}
.res li{padding:.7rem 0;border-top:1px solid var(--line)}
.res a{font-weight:600}
.res .ex{margin:.25rem 0 0;color:var(--dim);font-size:.92rem}
.res mark{background:var(--acc);color:var(--bg);padding:0 .15em;border-radius:2px}
.emo{font-size:1.1em;line-height:1;font-family:"Apple Color Emoji","Segoe UI Emoji",
  "Noto Color Emoji",sans-serif}
.trunc{font-size:12.5px;color:#a30;margin-top:.4rem}
@media(prefers-color-scheme:dark){.trunc{color:var(--hot)}}
.edited{font-size:12.5px;color:var(--dim);margin-top:.5rem;font-style:italic}
/* vB drew a quote as a bordered box with the attribution in bold above it. */
blockquote.bbq{margin:.5rem 0;padding:.4rem .7rem;border:1px solid var(--line);
background:var(--card)}
blockquote.bbq cite{display:block;font-size:12.5px;color:var(--dim);font-style:normal;
font-weight:bold;border-bottom:1px solid var(--line);padding-bottom:.2rem;
margin-bottom:.3rem}
pre.bbc-code{margin:.5rem 0;padding:.5rem .7rem;background:var(--card);border:1px solid
var(--line);overflow-x:auto;font-size:.9em;white-space:pre-wrap}
pre.irclog{margin:.5rem 0;padding:.5rem .7rem;background:var(--card);border:1px solid
var(--line);border-left:3px solid var(--cat);overflow-x:auto;font-size:.9em;
line-height:1.45;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
white-space:pre-wrap;word-break:break-word}
ul.bbl,ol.bbl{margin:.4rem 0 .4rem 1.2rem;padding:0}
.bbc{text-align:center}
/* The page numbers were small bordered boxes, the current one filled. */
.pager{margin:1rem 0;font-size:12.5px}
.pager a,.pager span{display:inline-block;padding:.15rem .45rem;margin-right:.2rem;
border:1px solid var(--line);background:var(--card)}
.pager .cur{background:var(--bar) var(--barimg) repeat-x bottom left;
color:var(--barfg);border-color:var(--bar);font-weight:bold}
footer.foot{margin-top:2rem;background:var(--bar) var(--barimg) repeat-x bottom left;
color:var(--barfg);border:1px solid var(--line);padding:.5rem .6rem;font-size:12.5px}
footer.foot a{color:#cfe6f4}
footer.foot a:hover{color:var(--hot)}
table{max-width:100%;display:block;overflow-x:auto}
"""

# Pagefind's own stylesheet carries a light-only palette in a plain `:root`
# block — `--pagefind-ui-text:#393939` and friends.  It is linked AFTER
# style.css, so an identical `:root` override there loses the cascade on equal
# specificity and every result renders dark-grey-on-black.  These rules go
# INLINE on the search page, after that <link>, and the ones that collide with
# Pagefind's Svelte-scoped selectors (`.pagefind-ui__result-title.svelte-xxxx
# .pagefind-ui__result-link.svelte-xxxx`, four classes) are written long enough
# to match that weight and win on order.
# Pagefind stems what it indexes, so `tac` also answers for `tacca` and there is
# no option to turn that off.  The search page therefore drives Pagefind's JS
# API directly instead of its stock UI: whatever the reader quotes ("tac") or
# marks with a plus (+tac) is checked again, verbatim and on a word boundary,
# against the page's own text before the result is shown.  Everything else keeps
# behaving like a normal fuzzy search.
SEARCH_JS = """\
<script type="module">
const box = document.getElementById("q");
const out = document.getElementById("res");
const note = document.getElementById("note");
const pf = await import("../pagefind/pagefind.js");
await pf.options({ bundlePath: "../pagefind/" });
let token = 0;

// `"due parole"` and `+parola` both mean: exactly this, no stemming.
function parse(raw) {
  const exact = [];
  let q = raw.replace(/"([^"]+)"/g, (_m, p) => { exact.push(p.trim()); return " " + p + " "; });
  q = q.replace(/(^|\\s)\\+(\\S+)/g, (_m, s, w) => { exact.push(w); return s + w; });
  return { q: q.trim(), exact: exact.filter(Boolean) };
}

function wordRx(term) {
  const esc = term.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&").replace(/\\s+/g, "\\\\s+");
  try {
    return new RegExp("(?<![\\\\p{L}\\\\p{N}_])" + esc + "(?![\\\\p{L}\\\\p{N}_])", "iu");
  } catch (e) {                      // no lookbehind: fall back to a loose match
    return new RegExp(esc, "iu");
  }
}

function card(d) {
  const sub = (d.sub_results && d.sub_results[0]) || null;
  const href = sub ? sub.url : d.url;
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = href;
  a.textContent = d.meta && d.meta.title ? d.meta.title : d.url;
  const p = document.createElement("p");
  p.className = "ex";
  p.innerHTML = (sub ? sub.excerpt : d.excerpt) || "";
  li.append(a, p);
  return li;
}

async function run() {
  const mine = ++token;
  const raw = box.value.trim();
  out.replaceChildren();
  if (raw.length < 2) { note.textContent = ""; return; }
  const { q, exact } = parse(raw);
  note.textContent = "cerco...";
  const search = await pf.search(q);
  if (mine !== token) return;
  const rx = exact.map(wordRx);
  const kept = [];
  let scanned = 0;
  for (const r of search.results) {
    if (mine !== token) return;
    if (kept.length >= 30) break;
    const d = await r.data();
    scanned++;
    if (rx.length && !rx.every((x) => x.test(d.raw_content || ""))) continue;
    kept.push(d);
    out.append(card(d));
  }
  if (mine !== token) return;
  const more = search.results.length > scanned ? " (primi " + scanned + " esaminati)" : "";
  note.textContent = kept.length
    ? kept.length + " risultati" + (rx.length ? ", filtrati esatti" : "") + more
    : "nessun risultato" + (rx.length ? " con la parola esatta" : "");
}

let t;
box.addEventListener("input", () => { clearTimeout(t); t = setTimeout(run, 250); });
box.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(t); run(); } });
const pre = new URLSearchParams(location.search).get("q");
if (pre) { box.value = pre; run(); }
</script>
"""

PAGEFIND_CSS = """\
<style>
:root{--pagefind-ui-primary:var(--acc);--pagefind-ui-text:var(--fg);
--pagefind-ui-background:var(--card);--pagefind-ui-border:var(--line);
--pagefind-ui-tag:var(--line);--pagefind-ui-border-width:1px;
--pagefind-ui-border-radius:6px;--pagefind-ui-font:inherit;
--pagefind-ui-scale:.9}
.pagefind-ui .pagefind-ui__form .pagefind-ui__search-input{background:var(--card);
color:var(--fg);border:1px solid var(--line)}
.pagefind-ui .pagefind-ui__form .pagefind-ui__search-input::placeholder{
color:var(--dim);opacity:1}
.pagefind-ui .pagefind-ui__form .pagefind-ui__search-clear{background:var(--card);
color:var(--acc)}
.pagefind-ui .pagefind-ui__results-area .pagefind-ui__message{color:var(--fg)}
.pagefind-ui .pagefind-ui__results .pagefind-ui__result{border-top:1px solid
var(--line)}
.pagefind-ui .pagefind-ui__result .pagefind-ui__result-title
.pagefind-ui__result-link{color:var(--acc);font-weight:600}
.pagefind-ui .pagefind-ui__result .pagefind-ui__result-excerpt{color:var(--fg)}
.pagefind-ui .pagefind-ui__result-excerpt mark{background:var(--acc);
color:var(--bg);padding:0 .12em;border-radius:2px;font-weight:600}
.pagefind-ui .pagefind-ui__result .pagefind-ui__result-tag{background:var(--line);
color:var(--fg)}
</style>"""

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
<header class="top" data-pagefind-ignore>
<a class="brand" href="{root}"><img src="{root}skin/logoforum.png" width="500"
height="96" alt="Azzurra IRC Network Forum"></a>
<div class="bar">
<div class="site"><a href="{root}">Archivio forum Azzurra</a></div>
<div class="crumb">{crumb}</div>
<div class="find"><a href="{root}cerca/">cerca</a></div></div></header>
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

    # The skin's own images (`fetch_skin.sh`).  Copied whole rather than listed
    # here: the CSS is the only thing that decides which of them matter, and a
    # list in two places goes out of step the first time one changes.
    for src in sorted(SKIN_DIR.rglob("*")) if SKIN_DIR.is_dir() else ():
        if not src.is_file():
            continue
        dst = out / "skin" / src.relative_to(SKIN_DIR)
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    # The recovered GIFs ride along with the pages: rendering is the only step
    # that knows which ones exist, and the site has to be self-contained.
    load_smilies()
    if SMILEY_FILES:
        for src in SMILEY_DIR.rglob("*"):
            if src.is_file():
                dst = out / "smilies" / src.relative_to(SMILEY_DIR)
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())

    load_assets()
    for name in ASSET_FILES:
        src, dst = ASSET_DIR / name, out / "assets" / name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

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
    # Index for the internal-link rewrite: thread pages are siblings, so the
    # href from inside one of them is `../<id>-<slug>/`.
    THREAD_LINKS.clear()
    # Section listings live one level up from the thread pages: `forum/<slug>/`
    # against `thread/<id>-<slug>/`.
    FORUM_LINKS.clear()
    FORUM_LINKS.update({
        f["id"]: (f"../../forum/{fslug[f['id']]}/", fname[f["id"]])
        for f in forums
    })
    for t in db.execute("SELECT id, title, first_post_at FROM threads"):
        title = t["title"] or f"discussione {t['id']}"
        THREAD_LINKS[t["id"]] = (f"../{t['id']}-{slug(title)}/", title,
                                 t["first_post_at"] or "")
    POST_LINKS.clear()
    POST_LINKS.update({
        r["vb_post_id"]: (r["thread_id"], r["seq"])
        for r in db.execute("SELECT vb_post_id, thread_id, seq FROM posts "
                            "WHERE vb_post_id IS NOT NULL")
    })
    # The phpBB numbering the board used before vBulletin. It survives the merge
    # on the rows it came from, which is what makes a `viewtopic.php?t=` link
    # resolvable at all.
    OLD_THREADS.clear()
    OLD_THREADS.update({
        r["old_topic_id"]: r["id"]
        for r in db.execute("SELECT id, old_topic_id FROM threads "
                            "WHERE old_topic_id IS NOT NULL")
    })
    OLD_POSTS.clear()
    OLD_POSTS.update({
        r["old_post_id"]: (r["thread_id"], r["seq"])
        for r in db.execute("SELECT old_post_id, thread_id, seq FROM posts "
                            "WHERE old_post_id IS NOT NULL")
    })
    # A phpBB section has no id of its own in the schema: the threads that came
    # from it say which vB forum it became, and the majority wins — the same
    # vote `oldboard_merge.py` takes, recomputed here rather than stored twice.
    OLD_FORUMS.clear()
    fvotes: dict[int, collections.Counter] = collections.defaultdict(
        collections.Counter)
    for r in db.execute(
            "SELECT o.forum_id AS old_id, t.forum_id AS new_id FROM threads t "
            "JOIN old_topics o ON o.topic_id = t.old_topic_id "
            "WHERE t.forum_id IS NOT NULL AND o.forum_id IS NOT NULL"):
        fvotes[r["old_id"]][r["new_id"]] += 1
    OLD_FORUMS.update({old: tally.most_common(1)[0][0]
                       for old, tally in fvotes.items()})
    empty = 0
    for t in threads:
        posts = db.execute(
            "SELECT seq, username, posted_at, body_html, body_text, truncated "
            "FROM posts WHERE thread_id = ? ORDER BY seq",
            (t["id"],),
        ).fetchall()
        title = t["title"] or f"discussione {t['id']}"
        blocks = []
        # Only threads that actually carry a flattened quote header pay for the
        # sibling text — building it for all 6565 threads would be O(posts^2)
        # of string work to answer a question 104 files ask.
        norms = ([norm_text(p["body_text"] or "") for p in posts]
                 if any(RE_FLAT_HEAD.search(p["body_html"] or "") for p in posts)
                 else None)
        for i, p in enumerate(posts):
            trunc = ('<div class="trunc">[lo snapshot dell\'Archive si interrompe '
                     "qui: il messaggio e' incompleto]</div>" if p["truncated"] else "")
            # The post's OWN text is excluded: it matches itself, always.
            sib = " \n".join(n for j, n in enumerate(norms) if j != i) if norms else ""
            blocks.append(
                f'<article class="post" id="post-{p["seq"]}">'
                f'<header><span class="who">{esc(p["username"] or "anonimo")}</span>'
                f' &middot; {when(p["posted_at"])} &middot; '
                f'<a href="#post-{p["seq"]}">#{p["seq"]}</a></header>'
                f'<div class="body">'
                f'{body_html(p["body_html"], sib, p["posted_at"] or "")}</div>'
                f"{trunc}</article>"
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
          body=('<h2 class="tt">Cerca nell\'archivio</h2>'
                '<input id="q" type="search" autocomplete="off" '
                'placeholder="parole da cercare" aria-label="cerca">'
                '<p class="meta">Le virgolette o il <code>+</code> chiedono la '
                'parola <strong>esatta</strong>: <code>"tac"</code> o '
                '<code>+tac</code> non tirano su <em>tacca</em>. Senza, la '
                'ricerca resta larga.</p>'
                '<p class="meta" id="note"></p>'
                '<ol class="res" id="res"></ol>'
                '<noscript><p class="meta">La ricerca ha bisogno di JavaScript. '
                'Senza, si naviga dall\'<a href="../">indice dei forum</a>: '
                'ogni pagina e\' HTML statico.</p></noscript>'
                + SEARCH_JS))
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
