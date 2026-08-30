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


# Text codes left naked in the body by the lofi flattening.  Digits alone are
# never a smiley: `:07:` is a timestamp out of a pasted IRC log, and there are
# ~7000 of those.
RE_TEXT_SMILEY = re.compile(r":([a-z][a-z0-9_]{0,14}):", re.I)
RE_TAG_SPLIT = re.compile(r"(<[^>]*>)")


def _text_smiley(m: re.Match[str]) -> str:
    name = m.group(1).lower()
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
# `showthread.php?t=N` only: measured against the post dates, 202 of its 213
# resolvable targets are older than the post linking them (95%, i.e. right), but
# `forum/viewtopic.php?t=N` scores 12 of 39 (31%) — the phpBB board it came from
# numbered its topics in a different space, so those ids are NOT ours and are
# left alone.  Same for the ids that land on a thread newer than the link.
RE_OLD_THREAD = re.compile(
    r"https?://(?:www\.)?forum\.azzurra\.org/showthread\.php\?"
    r"(?P<q>[\w=&;%#.+-]{0,120})", re.I)
RE_Q_T = re.compile(r"(?:^|[&;])t=(\d+)", re.I)
RE_Q_P = re.compile(r"(?:^|[&;])p=(\d+)", re.I)
RE_OLD_POST = re.compile(
    r"https?://(?:www\.)?forum\.azzurra\.org/showpost\.php\?"
    r"(?P<q>[\w=&;%#.+-]{0,120})", re.I)

THREAD_LINKS: dict[int, tuple[str, str, str]] = {}   # id -> (href, title, first)
POST_LINKS: dict[int, tuple[int, int]] = {}          # vb post id -> (thread, seq)


def _local_href(m: re.Match[str], when_posted: str) -> tuple[str, int] | None:
    q = html.unescape(m.group("q")).replace("&amp;", "&")
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
    href, _title, first = THREAD_LINKS[tid]
    # A thread cannot be linked before it exists: that is a foreign id, not ours.
    if first and when_posted and first > when_posted:
        return None
    return href + (f"#post-{seq}" if seq else ""), tid


RE_A_HREF = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)


def internal_links(body: str, when_posted: str = "") -> str:
    """Repoint old board URLs at the local pages, in the href and in the text."""
    if "forum.azzurra.org" not in body:
        return body

    def fix_anchor(m: re.Match[str]) -> str:
        url, text = m.group(1), m.group(2)
        hit = RE_OLD_THREAD.match(url) or RE_OLD_POST.match(url)
        found = _local_href(hit, when_posted) if hit else None
        if not found:
            return m.group(0)
        new, tid = found
        # When the visible text is the dead URL itself, the thread title says
        # more than a link nobody can follow.
        if "azzurra.org" in RE_TAGS.sub("", text):
            text = esc(THREAD_LINKS[tid][1])
        return f'<a href="{new}">{text}</a>'

    body = RE_A_HREF.sub(fix_anchor, body)

    def fix_bare(m: re.Match[str]) -> str:
        found = _local_href(m, when_posted)
        if not found:
            return m.group(0)
        new, tid = found
        return f'<a href="{new}">{esc(THREAD_LINKS[tid][1])}</a>'

    body = RE_OLD_THREAD.sub(fix_bare, body)
    return RE_OLD_POST.sub(fix_bare, body)


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

    return "".join(
        in_tag(part) if part.startswith("<") else RE_OLD_ANY.sub(in_text, part)
        for part in RE_TAG_SPLIT.split(body)
    )


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


def body_html(raw: str, siblings: str = "", when_posted: str = "") -> str:
    """Entities first (BBCode hides inside `&#91;b&#93;`), then tags, then scrub."""
    body = bbcode(unescape_entities(raw))
    if siblings:
        body = flat_quotes(body, siblings)
    body = internal_links(sanitise(body), when_posted)
    # Colours last: irc_logs still has to see the line starts as text, and the
    # spans this emits would hide them.
    return mirc_colors(irc_logs(archive_links(body, when_posted)))


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
pre.irclog{margin:.5rem 0;padding:.5rem .7rem;background:var(--bg);border-left:3px solid
var(--line);border-radius:0 4px 4px 0;overflow-x:auto;font-size:.85em;line-height:1.45;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
white-space:pre-wrap;word-break:break-word}
ul.bbl,ol.bbl{margin:.4rem 0 .4rem 1.2rem;padding:0}
.bbc{text-align:center}
.pager{margin:1.2rem 0;font-size:.9rem}
.pager a,.pager span{display:inline-block;padding:.15rem .45rem}
.pager .cur{background:var(--line);border-radius:4px}
footer.foot{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:.8rem;
font-size:.8rem;color:var(--dim)}
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
    # Index for the internal-link rewrite: thread pages are siblings, so the
    # href from inside one of them is `../<id>-<slug>/`.
    THREAD_LINKS.clear()
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
          extra=('<link rel="stylesheet" href="../pagefind/pagefind-ui.css">'
                 + PAGEFIND_CSS),
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
