#!/bin/bash
# Pull the Wayback CDX index WITHOUT collapse=urlkey, so every snapshot of every
# URL shows up (the collapsed index in cdx_all.txt keeps exactly one, which is
# why a page the Archive serves empty at that one timestamp had no fallback).
cd "$(dirname "$0")"
: > cdx_full.txt
for p in $(seq 0 19); do
  for try in 1 2 3; do
    if curl -s --max-time 300 \
      "https://web.archive.org/cdx/search/cdx?url=forum.azzurra.org*&filter=statuscode:200&fl=timestamp,original,mimetype,digest&page=$p" \
      -o "full_$p.txt" && [ -s "full_$p.txt" ] && ! grep -qi '<html' "full_$p.txt"; then
      cat "full_$p.txt" >> cdx_full.txt
      echo "full page $p ok $(wc -l < full_$p.txt) lines"
      break
    fi
    echo "full page $p retry $try"
    sleep 10
  done
done
echo "TOTAL $(wc -l < cdx_full.txt)"
