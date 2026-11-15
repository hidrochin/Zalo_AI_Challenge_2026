#!/usr/bin/env bash
# Serve Qwen/Qwen3.5-9B with vLLM on a rented GPU box (1 or 2 GPUs), the same flags as kaggle/run_kaggle.py.
# The server listens on 127.0.0.1 only: reach it through an SSH tunnel (ssh -N -L 18000:localhost:18000 ...).
#
# Usage (on the server):  bash serve_vllm.sh [--replace] [--foreground]
#   --replace     stop a vLLM that already runs on this box (it holds the GPUs) before starting
#   --foreground  stay attached (Ctrl-C stops the server); default: run in the background, log to $LOG
# Env overrides: MODEL, PORT, DTYPE, MAX_LEN, MAX_SEQS, GPU_UTIL, TP, VLLM_VERSION, VENV, DOWNLOAD_DIR, HF_TOKEN
set -euo pipefail

MODEL=${MODEL:-Qwen/Qwen3.5-9B}
PORT=${PORT:-18000}   # vast: Caddy holds 8000 (it proxies 8000 -> 18000 with the instance token), Jupyter 8080
DTYPE=${DTYPE:-auto}            # bf16 on Ada/Blackwell; Kaggle used half only because T4 lacks bf16
MAX_LEN=${MAX_LEN:-32768}
TP=${TP:-$(nvidia-smi -L | wc -l | tr -d ' ')}   # every GPU of the box
# Throughput on 2x RTX 4090 (no NVLink): TP=2 leaves ~22 GB of KV cache (~660k tokens of this hybrid model), so ~40
# prompts of 15k tokens fit at once; one replica per GPU would leave ~3 GB of KV cache next to 18 GB of bf16 weights.
# 64 seqs left the KV cache at most 49% used while runs queued at 64; 128 fits.
# One RTX 5090 (32 GB) leaves ~8 GB of KV cache next to the weights (~250k tokens, ~16 prompts of 15k), so fewer seqs.
MAX_SEQS=${MAX_SEQS:-$([ "$TP" -ge 2 ] && echo 128 || echo 48)}
# chunked-prefill token budget per step: prompts are 13-16k tokens. 32768 ran out of GPU memory under full load on
# 2x4090 (2026-11-14: a 704 MB activation with 228 MB free), so keep 16384; half of it on one GPU
MAX_BATCHED=${MAX_BATCHED:-$([ "$TP" -ge 2 ] && echo 16384 || echo 8192)}
GPU_UTIL=${GPU_UTIL:-0.92}
VLLM_VERSION=${VLLM_VERSION:-0.32.0}   # when no vLLM is installed: the version of the rented runs and of the image base
# the vast vLLM image ships one in /venv/main: use it rather than installing another
VENV=${VENV:-$([ -x /venv/main/bin/vllm ] && echo /venv/main || echo /workspace/vllm-env)}
SPEC=${SPEC:-}   # e.g. '{"method":"mtp","num_speculative_tokens":2}' when the checkpoint has MTP weights
DOWNLOAD_DIR=${DOWNLOAD_DIR:-/workspace/models}   # where the vast vLLM template keeps the weights: no second download
LOG=${LOG:-/workspace/vllm_${PORT}.log}
PIDFILE=/workspace/vllm_${PORT}.pid

REPLACE=0
FOREGROUND=0
for a in "$@"; do
  case "$a" in
    --replace) REPLACE=1 ;;
    --foreground) FOREGROUND=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

say() { echo "[$(date +%H:%M:%S)] $*"; }

# 1. A vLLM already running holds the GPUs
running=$(pgrep -af "vllm serve|vllm.entrypoints|VLLM::" || true)
if [ -n "$running" ]; then
  say "vLLM already running on this box:"
  echo "$running" | cut -c1-200
  if [ "$REPLACE" != 1 ] && curl -sf -m 5 "http://127.0.0.1:${PORT}/v1/models" | grep -q '"id":"llm"'; then
    say "port ${PORT} already serves 'llm'; nothing to do (use --replace to restart it)"
    exit 0
  fi
  if [ "$REPLACE" != 1 ]; then
    say "it holds GPU memory; rerun with --replace to stop it"
    exit 1
  fi
  if command -v supervisorctl >/dev/null 2>&1; then  # vast templates run vLLM under supervisor, which would restart it
    for prog in $(supervisorctl status 2>/dev/null | awk 'tolower($1) ~ /vllm/ {print $1}'); do
      say "supervisorctl stop $prog"
      supervisorctl stop "$prog" || true
    done
  fi
  pkill -f "vllm serve|vllm.entrypoints" || true
  for _ in $(seq 30); do pgrep -f "VLLM::|vllm serve|vllm.entrypoints" >/dev/null || break; sleep 2; done
  pkill -9 -f "VLLM::|vllm serve|vllm.entrypoints" || true
  sleep 3
fi

# 2. vLLM in its own venv, once
if [ ! -x "$VENV/bin/vllm" ]; then
  command -v uv >/dev/null 2>&1 || pip install -q uv
  say "installing vllm==${VLLM_VERSION} into $VENV"
  uv venv -q --python 3.12 "$VENV"
  uv pip install -q --python "$VENV/bin/python" "vllm==${VLLM_VERSION}"
fi
"$VENV/bin/python" -c 'import vllm, torch; print("vllm", vllm.__version__, "torch", torch.__version__, "cuda", torch.version.cuda)
assert torch.cuda.is_available(), "torch sees no GPU: the driver is older than this wheel'"'"'s CUDA (see nvidia-smi)"'
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader

# 3. Serve
ARGS=(serve "$MODEL" --served-model-name llm --host 127.0.0.1 --port "$PORT"
      --tensor-parallel-size "$TP" --gpu-memory-utilization "$GPU_UTIL"
      --max-num-seqs "$MAX_SEQS" --max-num-batched-tokens "$MAX_BATCHED" --enable-prefix-caching --seed 0
      --dtype "$DTYPE" --download-dir "$DOWNLOAD_DIR"
      --reasoning-parser qwen3 --language-model-only --max-model-len "$MAX_LEN")  # no vision weights: more KV cache
[ -n "$SPEC" ] && ARGS+=(--speculative-config "$SPEC")
say "$VENV/bin/vllm ${ARGS[*]}"
if [ "$FOREGROUND" = 1 ]; then
  exec "$VENV/bin/vllm" "${ARGS[@]}"
fi
nohup "$VENV/bin/vllm" "${ARGS[@]}" >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
say "started pid $(cat "$PIDFILE"), log $LOG; waiting for /health (downloads about 19 GB if $DOWNLOAD_DIR lacks the model)"

t0=$(date +%s)
until curl -sf -m 5 "http://127.0.0.1:${PORT}/health" >/dev/null; do
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    say "server exited; last log lines:"
    grep -v "it/s]" "$LOG" | tail -30
    exit 1
  fi
  sleep 10
done
say "healthy after $(( $(date +%s) - t0 )) s"

# 4. Sanity request, thinking off (as in kaggle/run_kaggle.py)
curl -s -m 300 "http://127.0.0.1:${PORT}/v1/chat/completions" -H 'Content-Type: application/json' -d '{
  "model": "llm", "temperature": 0, "max_tokens": 20,
  "messages": [{"role": "user", "content": "What is 17+25? Reply with the number only."}],
  "chat_template_kwargs": {"enable_thinking": false}}' | "$VENV/bin/python" -c \
  'import json,sys; r=json.load(sys.stdin); print("sanity reply:", repr(r["choices"][0]["message"]["content"]))'
say "stop with: kill \$(cat $PIDFILE)"
