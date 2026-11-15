#!/usr/bin/env bash
# Build the submission image: TEAM=<lowercase team id> docker/build.sh
# The code comes from the committed HEAD of the current branch (git archive HEAD), never from the working tree, so untracked
# or uncommitted files (public-test labels, debug notes, experiments) cannot reach the image.
set -euo pipefail

TEAM=${TEAM:?set TEAM=<lowercase team id>}
TAG="zaic2026_datamate_${TEAM}:final"
MODEL_REPO=Qwen/Qwen3.5-9B
MODEL_REV=c202236235762e1c871ad0ccb60c8ee5ba337b9a
ROOT=$(git rev-parse --show-toplevel)
CTX=${CTX:-$ROOT/build/context}
cd "$ROOT"

echo "building from branch $(git branch --show-current) at $(git rev-parse --short HEAD)"
git diff --quiet HEAD -- || { echo "uncommitted changes: the image is built from HEAD only; commit first"; exit 1; }
mkdir -p "$CTX/models" "$CTX/training_code/data"

# 1. model weights at a fixed revision (downloaded once)
W="$CTX/models/Qwen3.5-9B"
[ -f "$W/config.json" ] || uvx --from huggingface_hub hf download "$MODEL_REPO" --revision "$MODEL_REV" --local-dir "$W"
rm -rf "$W/.cache"

# 2. code: committed files only; predict.sh and README.md at the root of /code
rm -rf "$CTX/code" && mkdir -p "$CTX/code"
git archive HEAD src assets tests pyproject.toml uv.lock docker | tar -x -C "$CTX/code"
mv "$CTX/code/docker/predict.sh" "$CTX/code/docker/README.md" "$CTX/code/"
mv "$CTX/code/docker/Dockerfile" "$CTX/Dockerfile"
mkdir -p "$CTX/training_code/data_generation"
mv "$CTX/code/docker/training_code/README.md" "$CTX/training_code/README.md"
mv "$CTX/code/docker/manifest.py" "$CTX/training_code/data_generation/manifest.py"
rm -rf "$CTX/code/docker"
uv export --frozen --no-dev --no-hashes --no-emit-project > "$CTX/code/requirements.txt"
cp "$CTX/code/requirements.txt" "$CTX/training_code/requirements.txt"

# 3. train data (packaged as training data; its questions and answers are also the runtime example bank)
rsync -a --delete data/train/ "$CTX/training_code/data/train/"

# 4. guard: no public-test question id or label file in the code
if [ -f data/public_test/questions.jsonl ]; then
    ids=$(mktemp)
    python3 -c "import json,sys; [print(json.loads(l)['id']) for l in open(sys.argv[1]) if l.strip()]" \
        data/public_test/questions.jsonl > "$ids"
    if grep -rlF -f "$ids" "$CTX/code"; then echo "public-test ids found in the code (files above)"; exit 1; fi
    rm -f "$ids"
fi
[ ! -e "$CTX/code/ground_truth.jsonl" ] && [ ! -e "$CTX/code/assets/oracle" ] || { echo "public-test files in the code"; exit 1; }

# 5. manifest (sizes and SHA-256 of data and weights)
python3 "$CTX/training_code/data_generation/manifest.py" "$CTX" > "$CTX/training_code/data_manifest.json"

# 6. build and inspect
docker build --platform linux/amd64 -t "$TAG" "$CTX"
docker image inspect -f '{{.Os}}/{{.Architecture}} {{.Size}}' "$TAG"
docker run --rm --network none --entrypoint "" "$TAG" bash -c \
    'cd /code && find . -maxdepth 2 -not -path "./models/*/*" -not -path "./.venv/*" -not -path "./training_code/data/*" | sort'
echo "built $TAG from $(git rev-parse --short HEAD)"
