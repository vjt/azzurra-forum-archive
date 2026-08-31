#!/bin/bash
# fetch_smilies.sh — the board's own smiley GIFs, straight from the Archive.
# The CDX prefix query already gave us a timestamp per file, so this is a plain
# slow loop: one request in flight, a pause, a back-off when it starts failing.
cd "$(dirname "$0")"
DELAY=${DELAY:-3}
COOL=${COOL:-120}
n=0; got=0; bad=0; streak=0
while IFS=$'\t' read -r ts url rel; do
  [ -z "$rel" ] && continue
  out="smilies/$rel"
  [ -s "$out" ] && continue
  mkdir -p "$(dirname "$out")"
  n=$((n + 1))
  curl -sL --max-time 90 -o "$out.part" "https://web.archive.org/web/${ts}id_/${url}"
  rc=$?
  if [ -s "$out.part" ] && ! head -c 200 "$out.part" | grep -qi '<html\|<!doctype'; then
    mv "$out.part" "$out"; got=$((got + 1)); streak=0
  else
    rm -f "$out.part"; bad=$((bad + 1)); streak=$((streak + 1))
  fi
  if [ $streak -ge 8 ]; then echo "backing off ${COOL}s"; sleep "$COOL"; streak=0; fi
  [ $((n % 25)) -eq 0 ] && echo "tried=$n got=$got bad=$bad"
  sleep "$DELAY"
done < "${TARGETS:-smilies.tsv}"
echo "DONE tried=$n got=$got bad=$bad files=$(find smilies -type f | wc -l)"
