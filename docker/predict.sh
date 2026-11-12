#!/usr/bin/env bash
# Entry point: /data (one test split, read-only) -> /result/submission.jsonl and /result/logs/<id>.json.
# Every setting is fixed here; the script takes no arguments. Answers are written one by one as they are found,
# so a run stopped at the time limit keeps what it has. If the model server dies, it is restarted and the run resumes.
set -uo pipefail

MODEL=/code/models/Qwen3.5-9B
PORT=8000
WORK=/tmp/datamate  # /data is read-only: split links, ingest cache, DuckDB stores, compiled macros
RESULT=/result
MAX_STARTS=4
SERVE_ARGS=(--served-model-name llm --host 127.0.0.1 --port "$PORT" --seed 0
            --tensor-parallel-size 2 --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.90
            --reasoning-parser qwen3 --limit-mm-per-prompt '{"image":0,"video":0}')
RUN_ARGS=(--backend local --engine sql --model llm --base-url "http://127.0.0.1:$PORT/v1"
          --thinking on --max-tokens 8192 --concurrency 16 --exec-workers 4 --seed 0
          --catalog compiled --shop-views --judge unans)

log() { echo "[predict.sh] $(date -u +%FT%TZ) $*" | tee -a "$RESULT/predict.log"; }

mkdir -p "$RESULT/logs" "$WORK/root"
ln -sfn /data "$WORK/root/test"                          # the pipeline reads <data-root>/<split>/
ln -sfn /code/training_code/data/train "$WORK/root/train"  # train questions + answers: the example bank
NQ=$(grep -c . /data/questions.jsonl)
log "start: $NQ questions, $(nvidia-smi --query-gpu=name --format=csv,noheader | paste -sd, -)"

SERVER=""
stop_server() {
    [ -n "$SERVER" ] && kill "$SERVER" 2>/dev/null && wait "$SERVER" 2>/dev/null
    SERVER=""
}
trap stop_server EXIT

start_server() {
    vllm serve "$MODEL" "${SERVE_ARGS[@]}" >> "$RESULT/vllm.log" 2>&1 &
    SERVER=$!
    local t0=$SECONDS
    while (( SECONDS - t0 < 1800 )); do
        python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=5)" 2>/dev/null \
            && { log "model server up after $(( SECONDS - t0 ))s"; return 0; }
        kill -0 "$SERVER" 2>/dev/null || { log "model server exited"; tail -40 "$RESULT/vllm.log"; return 1; }
        sleep 5
    done
    log "model server not up after 30 min"
    return 1
}

answered() { [ -f "$RESULT/submission.jsonl" ] && grep -c . "$RESULT/submission.jsonl" || echo 0; }

for (( start = 1; start <= MAX_STARTS; start++ )); do
    if start_server; then
        (cd /code && .venv/bin/datamate --data-root "$WORK/root" --cache-dir "$WORK/cache/ingest" run --split test \
            --examples /code/assets/examples.jsonl --out "$RESULT" "${RUN_ARGS[@]}") 2>&1 | tee -a "$RESULT/predict.log"
    fi
    n=$(answered)
    log "start $start: $n/$NQ answered"
    (( n >= NQ )) && break
    stop_server
done
log "done: $(answered)/$NQ answered"
