#!/bin/bash
# fetch_skin.sh — pull the board's own skin images out of the Archive.
#
# The Azzurra3.0 stylesheet is embedded in every snapshot we hold (vBulletin
# inlines it in a <style id="vbulletin_css"> block), so the palette needed no
# fetching at all. The images it points at do: they were served from
# `forum.azzurra.org/azzurra3.0/` and that host has been down since 2016.
#
# This is a handful of files, not a crawl — but the Archive is the same Archive,
# so it keeps the discipline of fetch_assets.sh: one request in flight, a pause
# between them, a CDX probe before each fetch, and an HTML sniff on the result
# because the Archive answers a miss with a 4 KB error page, not a 404.
#
# Resumable: a file already on disk is skipped. Files land under skin/ with
# their original path, which is what the CSS refers to.
cd "$(dirname "$0")"
DELAY=${DELAY:-3}
BASE=http://forum.azzurra.org

# Only what the renderer actually uses. The board shipped a few dozen images
# (reputation pips, IM buttons, poll bars); those belong to functions this
# mirror does not have, and fetching them would be politeness spent on nothing.
FILES="
azzurra3.0/logoforum.png
azzurra3.0/sfondotb.png
azzurra3.0/misc/cat_back.png
azzurra3.0/misc/navbits_start.gif
azzurra3.0/misc/navbits_finallink_ltr.gif
azzurra3.0/statusicon/post_old.gif
"

got=0; miss=0
for f in $FILES; do
  out="skin/${f#azzurra3.0/}"
  [ -s "$out" ] && { echo "have $out"; continue; }
  mkdir -p "$(dirname "$out")"
  url="$BASE/$f"

  ts=$(curl -s --max-time 60 \
      "http://web.archive.org/cdx/search/cdx?url=${url#*://}&output=text&fl=timestamp&limit=1&filter=statuscode:200&filter=!mimetype:text/html" \
      | grep -m1 -x '[0-9]\{14\}')
  sleep "$DELAY"
  if [ -z "$ts" ]; then
    echo "MISS (no snapshot) $f"
    miss=$((miss + 1))
    continue
  fi

  curl -sL --max-time 120 -o "$out.part" "https://web.archive.org/web/${ts}id_/${url}"
  if [ -s "$out.part" ] && ! head -c 200 "$out.part" | grep -qi '<html\|<!doctype'; then
    mv "$out.part" "$out"
    echo "got $out ($(stat -c%s "$out") bytes, snapshot $ts)"
    got=$((got + 1))
  else
    rm -f "$out.part"
    echo "MISS (empty or HTML) $f"
    miss=$((miss + 1))
  fi
  sleep "$DELAY"
done
echo "DONE got=$got miss=$miss"
