#!/bin/bash
# Pull the vBulletin lo-fi archive pages for forum.azzurra.org from the Wayback Machine.
# Input:  targets.tsv  (timestamp \t original-url \t outfile)
# Output: pages/<outfile>   — raw HTML, one snapshot per URL (id_ = unmodified original)
# Resumable: an existing non-empty outfile is skipped.
cd "$(dirname "$0")"
mkdir -p pages
ok=0; skip=0; fail=0
while IFS=$'\t' read -r ts url out; do
  [ -z "$out" ] && continue
  if [ -s "pages/$out" ]; then skip=$((skip+1)); continue; fi
  for try in 1 2 3; do
    code=$(curl -s --max-time 120 -o "pages/$out.part" -w '%{http_code}' \
      "https://web.archive.org/web/${ts}id_/${url}")
    if [ "$code" = "200" ] && [ -s "pages/$out.part" ]; then
      mv "pages/$out.part" "pages/$out"; ok=$((ok+1)); break
    fi
    rm -f "pages/$out.part"
    sleep $((try * 5))
    [ "$try" = 3 ] && { fail=$((fail+1)); echo "FAIL $code $url"; }
  done
  if [ $(( (ok + skip) % 100 )) -eq 0 ]; then echo "progress ok=$ok skip=$skip fail=$fail"; fi
done < targets.tsv
echo "DONE ok=$ok skip=$skip fail=$fail"
