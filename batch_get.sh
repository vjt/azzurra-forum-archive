#!/bin/bash
# Pull the archived pages in batches through ONE curl process per batch, so the
# TLS/HTTP2 connection is reused instead of reopening (which the Archive resets).
# Input: targets.tsv (timestamp \t url \t outfile). Resumable: skips existing files.
cd "$(dirname "$0")"
mkdir -p pages
BATCH=${BATCH:-150}
PAR=${PAR:-4}
conf=$(mktemp)
n=0; done_=0
flush() {
  [ ! -s "$conf" ] && return
  curl -sL --parallel --parallel-max "$PAR" --parallel-immediate \
       --retry 2 --retry-delay 5 --max-time 120 --compressed -K "$conf"
  : > "$conf"
  # promote only non-empty downloads
  for p in pages/*.part; do
    [ -e "$p" ] || continue
    if [ -s "$p" ]; then mv "$p" "${p%.part}"; else rm -f "$p"; fi
  done
  done_=$(ls pages | grep -c '\.html$')
  echo "batch flushed, pages=$done_"
  sleep 2
}
while IFS=$'\t' read -r ts url out; do
  [ -z "$out" ] && continue
  [ -s "pages/$out" ] && continue
  printf 'url = "https://web.archive.org/web/%sid_/%s"\noutput = "pages/%s.part"\n' \
    "$ts" "$url" "$out" >> "$conf"
  n=$((n+1))
  if [ $((n % BATCH)) -eq 0 ]; then flush; fi
done < targets.tsv
flush
rm -f "$conf"
echo "DONE pages=$(ls pages | grep -c '\.html$')"
