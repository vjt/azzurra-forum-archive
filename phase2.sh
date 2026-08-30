#!/bin/bash
# Wait for phase 1 to finish, then pull the 807 threads that exist only in showthread.php.
cd "$(dirname "$0")"
while pgrep -f '^bash batch_get.sh$' >/dev/null; do sleep 60; done
echo "=== phase 2 start, $(ls pages | grep -c '\.html$') pages from phase 1"
TARGETS=targets_st.tsv BATCH=50 bash batch_get2.sh
