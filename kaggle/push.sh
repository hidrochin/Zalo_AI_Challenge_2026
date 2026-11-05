#!/usr/bin/env bash
# Upload code + data as a private Kaggle dataset, then push and run the kernel on 2x T4.
# Usage: kaggle/push.sh                     (first time: creates the dataset)
#        kaggle/push.sh update              (new dataset version with the current code)
#        DM_STAGES=train kaggle/push.sh update   (all 3000 train questions, scored against gold)
#        DM_KERNEL=datamate-public-run2 kaggle/push.sh update   (another kernel, so a running one is not replaced)
#        DM_ENGINE=sql kaggle/push.sh update     (the model writes SQL instead of pandas)
#        DM_DATASET=datamate-bundle-sql kaggle/push.sh   (own bundle per branch; `update` once it exists)
set -euo pipefail
cd "$(dirname "$0")/.."
USER=$(uvx --with six kaggle config view 2>/dev/null | awk '/username/{print $NF}')
USER=${USER:-hydrochii}
DATASET=${DM_DATASET:-datamate-bundle}
STAGE=outputs/kaggle/dataset
KERNEL=outputs/kaggle/kernel
rm -rf "$STAGE" "$KERNEL" && mkdir -p "$STAGE" "$KERNEL"

# which code a result came from: the run's outputs keep this file
{ git rev-parse --abbrev-ref HEAD; git rev-parse HEAD
  find src -name '*.py' | LC_ALL=C sort | xargs shasum | shasum; git status --short; } > outputs/kaggle/BUILD.txt
COPYFILE_DISABLE=1 tar -czf "$STAGE/bundle.tar.gz" --exclude '__pycache__' pyproject.toml uv.lock src assets data/train data/public_test \
  -C outputs/kaggle BUILD.txt
cat > "$STAGE/dataset-metadata.json" <<EOF
{"title": "$DATASET", "id": "$USER/$DATASET", "licenses": [{"name": "other"}]}
EOF
if [ "${1:-}" = "update" ]; then
  uvx --with six kaggle datasets version -p "$STAGE" -m "code update" -q
else
  uvx --with six kaggle datasets create -p "$STAGE" -q   # private by default
fi

# stages the kernel runs: DM_STAGES=train kaggle/push.sh update (default: public)
sed -e "s/os.environ.get(\"DM_STAGES\", \"public\")/os.environ.get(\"DM_STAGES\", \"${DM_STAGES:-public}\")/" \
    -e "s/os.environ.get(\"DM_ENGINE\", \"pandas\")/os.environ.get(\"DM_ENGINE\", \"${DM_ENGINE:-pandas}\")/" \
  kaggle/run_kaggle.py > "$KERNEL/run_kaggle.py"
cat > "$KERNEL/kernel-metadata.json" <<EOF
{"id": "$USER/${DM_KERNEL:-datamate-public-run}", "title": "${DM_KERNEL:-datamate-public-run}", "code_file": "run_kaggle.py",
 "language": "python", "kernel_type": "script", "is_private": true, "enable_gpu": true,
 "enable_internet": true, "dataset_sources": ["$USER/$DATASET"], "competition_sources": [], "kernel_sources": []}
EOF
echo "waiting for the dataset to finish processing..."
sleep 30
for _ in $(seq 1 60); do
  s=$(uvx --with six kaggle datasets status "$USER/$DATASET" 2>&1 | tail -1) || true  # a new dataset errors until it exists
  [ "$s" = "ready" ] && break
  sleep 20
done
echo "dataset: $s"
uvx --with six kaggle kernels push -p "$KERNEL" --accelerator NvidiaTeslaT4
