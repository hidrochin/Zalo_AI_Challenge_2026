#!/usr/bin/env bash
# Run one split against a remote OpenAI-compatible server with several shops at once.
# `datamate run` solves the questions of one shop in parallel but the shops one after another, so a single process leaves
# the server idle on small shops and during per-shop setup. This script:
#   1. runs `--prepare-only` once (rulebook compile, column roles, statuses, stores: all cached),
#   2. splits the shops into <groups> groups balanced by question count and runs one process per group,
#   3. merges the parts into <out>/submission.jsonl and <out>/logs/.
# Each part resumes on rerun (same out dir), like `datamate run`.
#
# Usage: remote/run_parallel.sh <split> <out_dir> <groups> <concurrency per group> [datamate run flags...]
# e.g.   remote/run_parallel.sh public_test outputs/rented/public_b 4 16 --thinking route --max-tokens 2048 \
#          --catalog delta --judge unans --context rich --frame --rewrite residual
# Env: BASE_URL (default http://localhost:18000/v1, the SSH tunnel to remote/serve_vllm.sh), EXEC_WORKERS (default 3)
set -euo pipefail

SPLIT=$1 OUT=$2 NGROUPS=$3 CONC=$4
shift 4
BASE_URL=${BASE_URL:-http://localhost:18000/v1}
EXEC_WORKERS=${EXEC_WORKERS:-3}
COMMON=(--split "$SPLIT" --base-url "$BASE_URL" --model llm --engine sql --exec-workers "$EXEC_WORKERS" "$@")
mkdir -p "$OUT"

curl -sf -m 10 "${BASE_URL}/models" | grep -q '"llm"' || { echo "no model 'llm' at $BASE_URL (tunnel up?)" >&2; exit 1; }

t0=$(date +%s)
echo "[$(date +%H:%M:%S)] prepare"
uv run datamate run "${COMMON[@]}" --prepare-only --out "$OUT/prepare" >"$OUT/prepare.log" 2>&1
echo "[$(date +%H:%M:%S)] prepared in $(( $(date +%s) - t0 )) s"

# longest-first greedy: each shop goes to the group with the fewest questions so far (counting only --ids-file ids if given)
IDS=""
prev=""
for a in "$@"; do [ "$prev" = "--ids-file" ] && IDS=$a; prev=$a; done
LINES=()  # macOS ships bash 3.2: no mapfile
while IFS= read -r line; do LINES+=("$line"); done < <(uv run python - "data/$SPLIT/questions.jsonl" "$NGROUPS" "$IDS" <<'EOF'
import json, sys
from collections import Counter
keep = set(open(sys.argv[3], encoding="utf-8").read().split()) if sys.argv[3] else None
n = Counter(q["business_id"] for q in map(json.loads, open(sys.argv[1], encoding="utf-8")) if keep is None or q["id"] in keep)
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

pids=()
for i in "${!LINES[@]}"; do
  read -r total bizs <<<"${LINES[$i]}"
  echo "[$(date +%H:%M:%S)] part$i: $total questions: $bizs"
  # shellcheck disable=SC2086
  uv run datamate run "${COMMON[@]}" --biz $bizs --concurrency "$CONC" --out "$OUT/part$i" >"$OUT/part$i.log" 2>&1 &
  pids+=($!)
done
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done

mkdir -p "$OUT/logs"
cat "$OUT"/part*/submission.jsonl >"$OUT/submission.jsonl"
for d in "$OUT"/part*/logs; do cp "$d"/*.json "$OUT/logs/" 2>/dev/null || true; done
echo "[$(date +%H:%M:%S)] done in $(( $(date +%s) - t0 )) s: $(wc -l <"$OUT/submission.jsonl") lines in $OUT/submission.jsonl (fail=$fail)"
exit $fail
