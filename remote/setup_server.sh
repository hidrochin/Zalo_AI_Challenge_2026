#!/usr/bin/env bash
# Prepare a freshly rented GPU box for runs, from the Mac: check the box, copy the code and the data, build the venv,
# then serve Qwen with remote/serve_vllm.sh in the background (TP = number of GPUs).
# Runs then go on the server itself, in tmux, with remote/run_parallel.sh (README "Status" has the run commands).
#
# Usage (on the Mac, key loaded with ssh-add):  remote/setup_server.sh <port> <host> [check|sync|serve|all]
#   check  GPU, driver, disk, Python only
#   sync   code (working tree, with its git HEAD in BUILD.txt) + data/train + data/public_test, then uv sync
#   serve  start vLLM, replacing one that already runs (logs to /workspace/vllm_18000.log)
#   all    (default) check, sync, serve
# Env: REMOTE_DIR (default /workspace/zalo_ai); serve_vllm.sh overrides (MAX_SEQS, MAX_BATCHED, GPU_UTIL, ...) pass through.
# ground_truth.jsonl never leaves the Mac: public scores are measured locally after rsync of the run back.
set -euo pipefail

PORT=${1:?usage: setup_server.sh <port> <host> [check|sync|serve|all]}
HOST=${2:?usage: setup_server.sh <port> <host> [check|sync|serve|all]}
STEP=${3:-all}
DIR=${REMOTE_DIR:-/workspace/zalo_ai}
RSH="ssh -p $PORT -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30"
R() { $RSH "root@$HOST" "$@"; }
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
say() { echo "[$(date +%H:%M:%S)] $*"; }

check() {
  R 'set -e
    nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader
    nvidia-smi | grep -o "CUDA Version: [0-9.]*"
    df -h /workspace 2>/dev/null | tail -1 || df -h / | tail -1
    free -g | head -2
    nproc
    python3 --version
    ls /venv/main/bin/vllm 2>/dev/null && /venv/main/bin/python -c "import vllm; print(\"template vllm\", vllm.__version__)" || echo "no template vLLM: serve installs one"'
}

sync() {
  git rev-parse HEAD > assets/BUILD.txt
  git status --porcelain >> assets/BUILD.txt
  say "code at $(git rev-parse --short HEAD)$(git diff --quiet HEAD -- || echo ' + uncommitted changes')"
  R "mkdir -p $DIR/data"
  rsync -az --delete -e "$RSH" src assets tests remote pyproject.toml uv.lock "root@$HOST:$DIR/"
  # home upload can be ~50 KB/s: SKIP_DATA=1 when the box already has the data (unzipped from a download there)
  [ "${SKIP_DATA:-0}" = 1 ] || rsync -az -e "$RSH" --exclude '*.zip' data/train data/public_test "root@$HOST:$DIR/data/"
  R "set -e
    export PATH=\$HOME/.local/bin:\$PATH
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH=\$HOME/.local/bin:\$PATH
    command -v tmux >/dev/null || (apt-get update -qq && apt-get install -y -qq tmux >/dev/null)
    cd $DIR && uv sync --frozen -q && uv run datamate --help >/dev/null && echo 'venv ready' && head -1 assets/BUILD.txt"
}

serve() {
  local envs=""
  for v in MODEL DTYPE MAX_LEN MAX_SEQS MAX_BATCHED GPU_UTIL TP VLLM_VERSION SPEC; do
    [ -n "${!v:-}" ] && envs+="$v=${!v} "
  done
  # serve_vllm.sh starts vLLM with nohup and waits for /health: up to ~15 min with the 19 GB download, so it runs
  # in tmux and this only starts it. --replace: the vast vLLM template already serves the model with 8 seqs
  R "cd $DIR && tmux kill-session -t vllm 2>/dev/null; tmux new-session -d -s vllm \"$envs bash remote/serve_vllm.sh --replace 2>&1 | tee /workspace/serve.log\" \
     && echo 'serve started: tail -f /workspace/serve.log (setup), /workspace/vllm_18000.log (server)'"
}

case "$STEP" in
  check) check ;;
  sync) sync ;;
  serve) serve ;;
  all) check; sync; serve ;;
  *) echo "unknown step: $STEP" >&2; exit 2 ;;
esac
