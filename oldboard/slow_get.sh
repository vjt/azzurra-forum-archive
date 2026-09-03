#!/bin/bash
# One request at a time, with a pause and a cooling-off when the Archive starts
# refusing. Measured 2026-08-30 00:40: after a few thousand requests in an hour
# web.archive.org stops answering this host — curl exits 7 (couldn't connect),
# and under the earlier parallel batches the same pressure showed up as a 200
# with a ZERO-LENGTH body, which is what left ~2360 pages "missing".
# So: slow, and it sleeps itself out of the hole instead of burning the list.
# Resumable: never refetches a file already on disk.
#
# 2026-09-03 02:45-03:05, measured: the refusal is per source address AND per
# port, and it moves. At 02:45 every connection to 207.241.237.3:443 was RST in
# 0.2 s while :80 answered 200; twenty minutes later the two had swapped, with
# :443 open and :80 refusing. The same IP answered on 443 from m42 throughout,
# so this is a rate-limit against this host, not an outage — and the port that
# is open is the one that has been quiet. Hence: try both schemes per target,
# and keep going.
#
# Two passes by default. A refused target is skipped, not retried in place;
# the next pass picks it up because the file is not on disk, which is also what
# makes the whole script resumable after a kill.
cd "$(dirname "$0")"
mkdir -p pages
DELAY=${DELAY:-3}
COOL=${COOL:-120}
PASSES=${PASSES:-2}
START_WAIT=${START_WAIT:-0}
[ "$START_WAIT" -gt 0 ] && sleep "$START_WAIT"

# 2026-09-03 03:05, measured: three of the first 225 pages came down as the
# Archive's own interstitial — `<title>Wayback Machine</title>`, 4.6 kB of
# JavaScript licence and no message at all — with HTTP 200 and a non-empty
# body, so the size test called them saved and the resume logic then skipped
# them forever. Same family as the zero-length 200 above: the failure arrives
# dressed as success, and only the content gives it away. A 1998 MHonArc page
# does not carry that title, so the test is safe.
#
# 2026-09-03 06:40, measured: two more pages of 200103 came down as a second
# error page the guard did not know, `<title>Internet Archive: Temporarily
# Offline</title>`, 11832 bytes with the logo inlined as base64 — big, HTTP
# 200, and no message. Both error pages are caught by title.
#
# `grep -a`: the pages are ISO-8859-1 and grep otherwise calls them binary.
fetch() {  # ts url dest -> 0 on a real, non-empty page
  local ts=$1 url=$2 dest=$3 scheme
  for scheme in https http; do
    curl -sL --max-time 90 -o "$dest.part" \
         "$scheme://web.archive.org/web/${ts}id_/${url}"
    rc=$?
    if [ -s "$dest.part" ] && \
       ! grep -qaE '<title>(Wayback Machine|Internet Archive: Temporarily Offline)</title>' \
         "$dest.part"; then
      mv "$dest.part" "$dest"
      return 0
    fi
    rm -f "$dest.part"
  done
  return 1
}

for pass in $(seq 1 "$PASSES"); do
  n=0; got=0; failed=0; streak=0
  while IFS=$'\t' read -r ts url out; do
    [ -z "$out" ] && continue
    [ -s "pages/$out" ] && continue
    n=$((n + 1))
    # A month index whose target was the bare `199904` landed as a *file* of
    # that name, and every one of the 38 messages under it then failed with
    # curl rc=23 because `pages/199904/` could not be created. Targets ending
    # in a directory name must say `index.html`; say so loudly rather than
    # spend a pass on a closed door.
    if ! mkdir -p "pages/$(dirname "$out")" 2>/dev/null; then
      echo "pass $pass: cannot make pages/$(dirname "$out") — a file is in the way, skipping $out"
      failed=$((failed + 1))
      continue
    fi
    if fetch "$ts" "$url" "pages/$out"; then
      got=$((got + 1)); streak=0
    else
      failed=$((failed + 1)); streak=$((streak + 1))
    fi
    # Five failures in a row on both schemes = the Archive is done with us for
    # now. Back off hard rather than spend the rest of the list against a
    # closed door.
    if [ $streak -ge 5 ]; then
      echo "pass $pass: backing off ${COOL}s after $streak failures (last rc=$rc)"
      sleep "$COOL"
      streak=0
    fi
    [ $((n % 50)) -eq 0 ] && \
      echo "pass $pass: tried=$n got=$got failed=$failed pages=$(find pages -name "*.html" | wc -l)"
    sleep "$DELAY"
  done < targets.tsv
  echo "PASS $pass DONE tried=$n got=$got failed=$failed pages=$(find pages -name "*.html" | wc -l)"
  [ "$n" -eq 0 ] && break   # nothing left to fetch: stop early
done
echo "DONE pages=$(find pages -name "*.html" | wc -l) of $(grep -c . targets.tsv) targets"
