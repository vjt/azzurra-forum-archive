#!/bin/bash
# One request at a time, with a pause and a cooling-off when the Archive starts
# refusing. Measured 2026-08-30 00:40: after a few thousand requests in an hour
# web.archive.org stops answering this host — curl exits 7 (couldn't connect),
# and under the earlier parallel batches the same pressure showed up as a 200
# with a ZERO-LENGTH body, which is what left ~2360 pages "missing".
# So: slow, and it sleeps itself out of the hole instead of burning the list.
# Resumable: never refetches a file already on disk.
cd "$(dirname "$0")"
mkdir -p pages
DELAY=${DELAY:-3}
COOL=${COOL:-120}
START_WAIT=${START_WAIT:-0}
[ "$START_WAIT" -gt 0 ] && sleep "$START_WAIT"

n=0; got=0; empty=0; refused=0; streak=0
while IFS=$'\t' read -r ts url out; do
  [ -z "$out" ] && continue
  [ -s "pages/$out" ] && continue
  n=$((n + 1))
  curl -sL --max-time 90 -o "pages/$out.part" \
       "https://web.archive.org/web/${ts}id_/${url}"
  rc=$?
  if [ -s "pages/$out.part" ]; then
    mv "pages/$out.part" "pages/$out"
    got=$((got + 1)); streak=0
  else
    rm -f "pages/$out.part"
    if [ $rc -eq 0 ]; then empty=$((empty + 1)); else refused=$((refused + 1)); fi
    streak=$((streak + 1))
  fi
  # Five failures in a row = the Archive is done with us for now. Back off hard
  # rather than spend the rest of the list against a closed door.
  if [ $streak -ge 5 ]; then
    echo "backing off ${COOL}s after $streak failures (last rc=$rc)"
    sleep "$COOL"
    streak=0
  fi
  [ $((n % 50)) -eq 0 ] && \
    echo "tried=$n got=$got empty=$empty refused=$refused pages=$(ls pages | grep -c '\.html$')"
  sleep "$DELAY"
done < "${TARGETS:-targets.tsv}"
echo "DONE tried=$n got=$got empty=$empty refused=$refused pages=$(ls pages | grep -c '\.html$')"
