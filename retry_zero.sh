#!/bin/bash
# Refetch the threads that landed on disk with zero posts.
#
# Unlike slow_get*.sh this does NOT skip a file that already exists in pages/:
# those pages are exactly the problem — they are cut mid-transfer, head-only, and
# a non-empty file is not a complete one. So everything lands in retry/ under
# `<name>.html.<timestamp>`, one file per snapshot, and pick_zero.py decides
# later which candidate (if any) beats what pages/ already holds.
#
# Same manners as slow_get.sh toward the Archive: one request at a time, a pause
# between them, and a hard back-off when it starts refusing. Resumable — a
# candidate already downloaded non-empty is not fetched again.
cd "$(dirname "$0")"
mkdir -p retry
DELAY=${DELAY:-4}
COOL=${COOL:-240}
START_WAIT=${START_WAIT:-0}
[ "$START_WAIT" -gt 0 ] && sleep "$START_WAIT"

n=0; got=0; empty=0; refused=0; streak=0
while IFS=$'\t' read -r ts url out; do
  [ -z "$out" ] && continue
  [ -s "retry/$out" ] && continue
  n=$((n + 1))
  curl -sL --max-time 120 -o "retry/$out.part" \
       "https://web.archive.org/web/${ts}id_/${url}"
  rc=$?
  if [ -s "retry/$out.part" ]; then
    mv "retry/$out.part" "retry/$out"
    got=$((got + 1)); streak=0
  else
    rm -f "retry/$out.part"
    if [ $rc -eq 0 ]; then empty=$((empty + 1)); else refused=$((refused + 1)); fi
    streak=$((streak + 1))
  fi
  if [ $streak -ge 5 ]; then
    echo "backing off ${COOL}s after $streak failures (last rc=$rc)"
    sleep "$COOL"
    streak=0
  fi
  [ $((n % 50)) -eq 0 ] && \
    echo "tried=$n got=$got empty=$empty refused=$refused files=$(ls retry | wc -l)"
  sleep "$DELAY"
done < "${TARGETS:-targets_zero.tsv}"
echo "DONE tried=$n got=$got empty=$empty refused=$refused files=$(ls retry | wc -l)"
