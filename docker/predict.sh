#!/usr/bin/env bash
# Entry point: /data (one test split, read-only) -> /result/submission.jsonl and /result/logs/<id>.json.
# Every setting is fixed here; the script takes no arguments. Answers are appended one by one as they are found, so a
# run stopped at the time limit keeps what it has. If the model server dies, it is restarted and the run resumes.
#
# Time: the organizers' 6 h include model loading. Questions not started by DEADLINE are left unanswered (abstain);
# from DEADLINE - DEEP_MARGIN no question takes the deep (thinking) tier.
# Throughput: one `datamate run` solves one shop at a time, so after a single prepare pass the shops are split into
# GROUPS processes balanced by question count; all of them append to /result/submission.jsonl.
set -uo pipefail

T0=$(date +%s)
DEADLINE=$(( T0 + 6 * 3600 - 25 * 60 ))   # 25 min of margin for the last questions in flight and the server stop
DEEP_MARGIN=3600
MODEL=/code/models/Qwen3.5-9B
PORT=8000
WORK=/tmp/datamate  # /data is read-only: split links, ingest cache, DuckDB stores
RESULT=/result
MAX_STARTS=4
GROUPS=12
SERVE_ARGS=(--served-model-name llm --host 127.0.0.1 --port "$PORT" --seed 0
            --tensor-parallel-size 2 --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.90
            --max-num-seqs 128 --max-num-batched-tokens 16384 --enable-prefix-caching
            --reasoning-parser qwen3 --language-model-only)
RUN_ARGS=(--backend local --engine sql --model llm --base-url "http://127.0.0.1:$PORT/v1" --seed 0
          --thinking route --max-tokens 2048 --catalog delta --judge unans --context rich --frame --rewrite residual
          --exec-workers 4 --deadline "$DEADLINE" --deep-margin "$DEEP_MARGIN")

log() { echo "[predict.sh] $(date -u +%FT%TZ) $*" | tee -a "$RESULT/predict.log"; }

mkdir -p "$RESULT/logs" "$WORK/root"
ln -sfn /data "$WORK/root/test"                          # the pipeline reads <data-root>/<split>/
ln -sfn /code/training_code/data/train "$WORK/root/train"  # train questions + answers: the example bank
NQ=$(grep -c . /data/questions.jsonl)
log "start: $NQ questions, $(nvidia-smi --query-gpu=name --format=csv,noheader | paste -sd, -), deadline $(date -u -d @"$DEADLINE" +%FT%TZ)"

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

datamate() { (cd /code && .venv/bin/datamate --data-root "$WORK/root" --cache-dir "$WORK/cache/ingest" "$@"); }

# shop groups, longest first to the emptiest group (one line per group: "<questions> <biz...>")
mapfile -t PARTS < <(python3 - /data/questions.jsonl "$GROUPS" <<'EOF'
import json, sys
from collections import Counter
n = Counter(json.loads(l)["business_id"] for l in open(sys.argv[1], encoding="utf-8") if l.strip())
groups = [[0, []] for _ in range(int(sys.argv[2]))]
for biz, k in sorted(n.items(), key=lambda it: (-it[1], it[0])):
    g = min(groups, key=lambda g: g[0])
    g[0] += k
    g[1].append(biz)
for total, bizs in groups:
    if bizs:
        print(total, " ".join(sorted(bizs)))
EOF
)

for (( start = 1; start <= MAX_STARTS; start++ )); do
    (( $(date +%s) < DEADLINE )) || { log "deadline reached"; break; }
    if start_server; then
        # one pass over every shop: rule checks, column roles, statuses, stores (cached for the groups)
        datamate run --split test --examples /code/assets/examples.jsonl --out "$WORK/prepare" "${RUN_ARGS[@]}" \
            --prepare-only >> "$RESULT/predict.log" 2>&1
        log "prepared after $(( $(date +%s) - T0 ))s"
        pids=()
        for i in "${!PARTS[@]}"; do
            read -r total bizs <<<"${PARTS[$i]}"
            # shellcheck disable=SC2086
            datamate run --split test --examples /code/assets/examples.jsonl --out "$RESULT" "${RUN_ARGS[@]}" \
                --biz $bizs --concurrency 10 >> "$RESULT/part$i.log" 2>&1 &
            pids+=($!)
        done
        for p in "${pids[@]}"; do wait "$p"; done
    fi
    n=$(answered)
    log "start $start: $n/$NQ answered"
    (( n >= NQ )) && break
    stop_server
done
log "done: $(answered)/$NQ answered after $(( $(date +%s) - T0 ))s"
