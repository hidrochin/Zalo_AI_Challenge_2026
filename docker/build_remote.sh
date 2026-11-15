#!/usr/bin/env bash
# Daemonless build of the submission image on the rented server, which has no docker: docker/assemble.py writes the
# `docker save` archive from the base image's blobs (pulled once into /workspace/imgbuild with crane) plus three layers:
# /code/models (Qwen3.5-9B), /code/training_code (train data + manifest) and /code (committed code + its .venv).
# The server's Python (Ubuntu 24.04, /usr/bin/python3.12) is the base image's, so a venv built there runs in the image.
#
# Usage, from the Mac, committed HEAD only:
#   SSH="ssh -p 10102 root@45.143.122.52" docker/build_remote.sh          # stage /code and its venv
#   SSH="ssh -p 10102 root@45.143.122.52" BUILD=1 docker/build_remote.sh  # then assemble (tmux `img`, about 25 min)
# The archive needs about 27 GB free in /workspace: delete the previous out/*.tar.gz once it is uploaded.
# Result: /workspace/imgbuild/out/zaic2026_datamate_oom.tar.gz, .md5, verify.json (tar_bytes = size of the .tar).
set -euo pipefail

SSH=${SSH:?set SSH="ssh -p <port> root@<host>"}
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
git diff --quiet HEAD -- || { echo "uncommitted changes: the image holds HEAD only; commit first"; exit 1; }
echo "staging $(git branch --show-current) at $(git rev-parse --short HEAD)"

git archive HEAD src assets tests pyproject.toml uv.lock docker | $SSH 'set -euo pipefail
  export PATH=$HOME/.local/bin:$PATH
  rm -rf /code.new && mkdir /code.new && tar -x -C /code.new
  cd /code.new && mv docker/predict.sh docker/README.md . && cp docker/assemble.py /workspace/imgbuild/assemble.py && rm -rf docker
  mkdir -p /code && find /code -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  cp -a /code.new/. /code/ && rm -rf /code.new
  cd /code && uv sync --frozen --python /usr/bin/python3.12 -q && uv export --frozen --no-hashes -q > requirements.txt
  .venv/bin/datamate --help > /dev/null && echo "/code ready: $(du -sh /code | cut -f1)"'

if [ "${BUILD:-0}" = 1 ]; then
  $SSH 'set -euo pipefail; cd /workspace/imgbuild && rm -f out/* && mkdir -p out && cat > build.sh <<"EOF"
set -euo pipefail
cd /workspace/imgbuild
N=zaic2026_datamate_oom
date -u +"start %FT%TZ"
SP=usr/local/lib/python3.12/dist-packages  # torchcodec: the RUN pip uninstall of docker/Dockerfile
python3 assemble.py --base . --tag $N:final \
  --remove $SP/torchcodec --remove $SP/torchcodec-0.17.0.dist-info --remove $SP/torchcodec.libs \
  --layer ctx/models=code/models:deref \
  --layer ctx/training_code=code/training_code --layer /code=code | pigz -p 64 > out/$N.tar.gz
date -u +"written %FT%TZ"
pigz -dc out/$N.tar.gz | python3 assemble.py --verify - > out/verify.json
cat out/verify.json
(cd out && md5sum $N.tar.gz > $N.tar.gz.md5 && cat $N.tar.gz.md5)
date -u +"done %FT%TZ"
EOF
  tmux new-session -d -s img "bash build.sh > build.log 2>&1" && echo "build started: tail -f /workspace/imgbuild/build.log"'
fi
