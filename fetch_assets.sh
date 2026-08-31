#!/bin/bash
# fetch_assets.sh — pull the posts' images out of the Archive, one at a time.
#
# Same discipline as slow_get_gap.sh: a single request in flight, a pause
# between them, and a hard back-off when the Archive starts refusing. The CDX
# probe comes first because guessing a year costs a fetch and answers nothing
# when there is no snapshot at all (measured: zeroserio.it was never captured).
#
# Resumable: a file already on disk, or a URL already known to have no
# snapshot, is skipped.
cd "$(dirname "$0")"
mkdir -p assets
: > assets_miss.tmp
DELAY=${DELAY:-3}
COOL=${COOL:-120}
MISS=${MISS:-assets_miss.txt}
touch "$MISS"

n=0; got=0; miss=0; refused=0; streak=0
while IFS=$'\t' read -r url out; do
  [ -z "$out" ] && continue
  [ -s "assets/$out" ] && continue
  grep -qxF "$url" "$MISS" && continue
  n=$((n + 1))

  # fl=timestamp in plain text: the JSON form put the timestamp after a comma,
  # not after the '[' the old sed anchored on, so it matched nothing and every
  # single URL was filed as "never captured" — 1122 of them, all false.
  ts=$(curl -s --max-time 60 \
      "http://web.archive.org/cdx/search/cdx?url=${url#*://}&output=text&fl=timestamp&limit=1&filter=statuscode:200&filter=!mimetype:text/html" \
      | grep -m1 -x '[0-9]\{14\}')
  sleep "$DELAY"

  if [ -z "$ts" ]; then
    # No snapshot at all is the normal answer for a host dead since 2007, not
    # the Archive pushing back: it must not feed the back-off, or a run of dead
    # hosts (the list is sorted, so they cluster) costs two minutes every eight.
    echo "$url" >> "$MISS"
    miss=$((miss + 1))
  else
    curl -sL --max-time 120 -o "assets/$out.part" \
         "https://web.archive.org/web/${ts}id_/${url}"
    rc=$?
    # An image is not HTML: the Archive's error page is, and it is 4 KB of it.
    if [ -s "assets/$out.part" ] && ! head -c 200 "assets/$out.part" | grep -qi '<html\|<!doctype'; then
      mv "assets/$out.part" "assets/$out"
      got=$((got + 1)); streak=0
    else
      rm -f "assets/$out.part"
      echo "$url" >> "$MISS"
      # A snapshot that exists but comes back empty/HTML is the Archive
      # refusing or rotting: that one does feed the back-off.
      if [ $rc -eq 0 ]; then miss=$((miss + 1)); else refused=$((refused + 1)); fi
      streak=$((streak + 1))
    fi
    sleep "$DELAY"
  fi

  if [ $streak -ge 8 ]; then
    echo "backing off ${COOL}s after $streak failures"
    sleep "$COOL"
    streak=0
  fi
  [ $((n % 25)) -eq 0 ] && \
    echo "tried=$n got=$got miss=$miss refused=$refused files=$(ls assets | wc -l)"
done < "${TARGETS:-assets.tsv}"
echo "DONE tried=$n got=$got miss=$miss refused=$refused files=$(ls assets | wc -l)"
