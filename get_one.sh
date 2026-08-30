#!/bin/bash
# One archived page. Args: timestamp url outfile. Resumable, 3 tries.
cd "$(dirname "$0")"
ts=$1; url=$2; out=$3
[ -s "pages/$out" ] && exit 0
for try in 1 2 3; do
  code=$(curl -sL --max-time 120 -o "pages/$out.part" -w '%{http_code}' \
    "https://web.archive.org/web/${ts}id_/${url}")
  if [ "$code" = "200" ] && [ -s "pages/$out.part" ]; then
    mv "pages/$out.part" "pages/$out"; exit 0
  fi
  rm -f "pages/$out.part"
  sleep $((try * 4))
done
echo "FAIL $code $url" >> failed.log
exit 1
