#!/usr/bin/env bash
# Export the built image for upload (guideline section 11): TEAM=<lowercase team id> docker/export.sh [out dir]
# Writes zaic2026_datamate_<team>.tar.gz, its .md5 file, and prints the uncompressed .tar size in bytes.
# Do not rename the archive after the .md5 file is written.
set -euo pipefail

TEAM=${TEAM:?set TEAM=<lowercase team id>}
OUT=${1:-.}
NAME="zaic2026_datamate_${TEAM}"
GZ=$(command -v pigz > /dev/null && echo "pigz -p 8" || echo gzip)
cd "$OUT"

docker save "${NAME}:final" | $GZ > "${NAME}.tar.gz"
SIZE=$($GZ -dc "${NAME}.tar.gz" | wc -c | tr -d ' ')
if command -v md5sum > /dev/null; then md5sum "${NAME}.tar.gz" > "${NAME}.tar.gz.md5"; else md5 -r "${NAME}.tar.gz" > "${NAME}.tar.gz.md5"; fi
echo "uncompressed .tar bytes: $SIZE (limit 100 GB = 100000000000)"
echo "md5: $(cat "${NAME}.tar.gz.md5")"
ls -l "${NAME}.tar.gz"
