#!/bin/bash
# Pull the full Wayback CDX index for forum.azzurra.org (20 pages).
cd "$(dirname "$0")"
: > cdx_all.txt
for p in $(seq 0 19); do
  for try in 1 2 3; do
    if curl -s --max-time 180 \
      "https://web.archive.org/cdx/search/cdx?url=forum.azzurra.org*&filter=statuscode:200&collapse=urlkey&fl=timestamp,original,mimetype,digest&page=$p" \
      -o "page_$p.txt" && [ -s "page_$p.txt" ] && ! grep -qi '<html' "page_$p.txt"; then
      cat "page_$p.txt" >> cdx_all.txt
      echo "page $p ok $(wc -l < page_$p.txt) lines"
      break
    fi
    echo "page $p retry $try"
    sleep 10
  done
done
echo "TOTAL $(wc -l < cdx_all.txt)"
