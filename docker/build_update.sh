#!/usr/bin/env bash
# Build the next submission image on a machine with Docker (the Mac: amd64 under emulation, no GPU needed) on top of
# the previous image, then export it. Code = HEAD's src/assets/tests + the working tree's docker/predict.sh and README.
#
# Usage: docker/build_update.sh [build|smoke|export]   (default build)
#   build   docker build -f docker/Dockerfile.update (PREV must be loaded: docker load -i <previous archive>, then
#           docker tag zaic2026_datamate_oom:final zaic2026_datamate_oom:v1)
#   smoke   no-GPU checks in the image: vLLM imports, datamate CLI, predict.sh on 5 questions writes a valid
#           (empty) submission and the shop groups, with no model server
#   export  docker/export.sh into OUT (default ~/Downloads/zaic2026/v2)
# Env: PREV (default zaic2026_datamate_oom:v1), TEAM (default oom)
set -euo pipefail

PREV=${PREV:-zaic2026_datamate_oom:v1}
TEAM=${TEAM:-oom}
TAG="zaic2026_datamate_${TEAM}:final"
STEP=${1:-build}
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

build() {
  docker image inspect "$PREV" > /dev/null || { echo "load the previous image and tag it $PREV first"; exit 1; }
  local ctx
  ctx=$(mktemp -d)
  trap 'rm -rf "$ctx"' RETURN
  mkdir -p "$ctx/code"
  git archive HEAD src assets tests pyproject.toml uv.lock | tar -x -C "$ctx/code"
  cp docker/predict.sh docker/README.md "$ctx/code/"
  { git rev-parse HEAD; git status --porcelain -- docker; } > "$ctx/code/BUILD.txt"
  echo "code: $(git rev-parse --short HEAD), docker files: $(git status --porcelain -- docker | wc -l | tr -d ' ') changed"
  docker build --platform linux/amd64 -f docker/Dockerfile.update --build-arg PREV="$PREV" -t "$TAG" "$ctx"
}

smoke() {
  local d
  d=$(mktemp -d)
  mkdir -p "$d/data" "$d/result"
  cp data/public_test/rulebook.md data/public_test/calendar.csv "$d/data/"
  cp -R data/public_test/data "$d/data/data"
  head -5 data/public_test/questions.jsonl > "$d/data/questions.jsonl"
  docker run --rm --platform linux/amd64 "$TAG" bash -c '
    python3 -c "import vllm.entrypoints.cli.main, vllm.multimodal.media.audio as a; print(\"vllm import ok, AudioDecoder\", a.AudioDecoder)"
    cd /code && .venv/bin/datamate --help | head -3 && cat BUILD.txt'
  # no GPU: vllm serve exits, predict.sh must still leave a regular /result/submission.jsonl and log its shop groups
  docker run --rm --platform linux/amd64 -v "$d/data:/data:ro" -v "$d/result:/result" "$TAG" \
    bash -c 'sed -i "s/^MAX_STARTS=.*/MAX_STARTS=1/" /code/predict.sh; timeout 1200 bash /code/predict.sh' 2>&1 \
    | grep '^\[predict.sh\]' || true
  echo "--- end of vllm.log (no GPU: expected to stop at device detection, not at an import)"
  grep -E "Error|error" "$d/result/vllm.log" | tail -4
  ls -l "$d/result/submission.jsonl" && test -f "$d/result/submission.jsonl" && ! test -L "$d/result/submission.jsonl" \
    && echo "smoke: submission.jsonl is a regular file"
  rm -rf "$d"
}

case "$STEP" in
  build) build ;;
  smoke) smoke ;;
  export) mkdir -p "${OUT:-$HOME/Downloads/zaic2026/v2}" && TEAM=$TEAM docker/export.sh "${OUT:-$HOME/Downloads/zaic2026/v2}" ;;
  *) echo "unknown step: $STEP" >&2; exit 2 ;;
esac
