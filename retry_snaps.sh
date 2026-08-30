#!/bin/bash
# Second pass over the pages the Archive served EMPTY at their one collapsed
# timestamp: walk every other snapshot of the same URL, oldest first, one round
# per snapshot index, until a round downloads nothing new.
#
# Inputs: targets.tsv (ts \t url \t outfile) + cdx_full.txt (uncollapsed index).
# Resumable: a page already on disk is never refetched.
cd "$(dirname "$0")"
mkdir -p pages
BATCH=${BATCH:-150}
PAR=${PAR:-4}
MAXR=${MAXR:-20}

# url \t ts ts ts…  (oldest first) — only for URLs we actually want.
tr -s ' ' '\t' < cdx_full.txt > cdx_full.tsv
awk -F'\t' 'FNR==NR{want[$2]=1; next} ($2 in want){s[$2]=s[$2]" "$1}
            END{for (u in s) printf "%s\t%s\n", u, substr(s[u], 2)}' \
    targets.tsv cdx_full.tsv | sort > snaps.tsv
echo "snaps.tsv: $(wc -l < snaps.tsv) urls with at least one snapshot"

conf=$(mktemp)
flush() {
  [ ! -s "$conf" ] && return
  curl -sL --parallel --parallel-max "$PAR" --parallel-immediate \
       --retry 2 --retry-delay 5 --max-time 120 --compressed -K "$conf"
  : > "$conf"
  for p in pages/*.part; do
    [ -e "$p" ] || continue
    if [ -s "$p" ]; then mv "$p" "${p%.part}"; else rm -f "$p"; fi
  done
  sleep 2
}

for r in $(seq 1 "$MAXR"); do
  before=$(ls pages | grep -c '\.html$')
  n=0
  while IFS=$'\t' read -r url out ts; do
    [ -z "$ts" ] && continue
    printf 'url = "https://web.archive.org/web/%sid_/%s"\noutput = "pages/%s.part"\n' \
      "$ts" "$url" "$out" >> "$conf"
    n=$((n + 1))
    [ $((n % BATCH)) -eq 0 ] && flush
  done < <(
    awk -F'\t' -v r="$r" '
      FNR==NR{split($0, a, "\t"); snap[a[1]]=a[2]; next}
      {
        out = $3
        if (system("test -s pages/" out) == 0) next
        if (!($2 in snap)) next
        k = split(snap[$2], t, " ")
        if (r > k) next
        printf "%s\t%s\t%s\n", $2, out, t[r]
      }' snaps.tsv targets.tsv
  )
  flush
  after=$(ls pages | grep -c '\.html$')
  echo "round $r: queued=$n pages $before -> $after"
  [ "$n" -eq 0 ] && break
done

rm -f "$conf"
echo "DONE pages=$(ls pages | grep -c '\.html$')"
