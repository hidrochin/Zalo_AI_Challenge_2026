# Submission image (organizers' Docker guideline). Built by docker/build.sh from a staged context:
#   models/         model weights at a fixed revision
#   training_code/  README, train data, data_manifest.json
#   code/           the committed `submission` branch (git archive), predict.sh and README.md at its root
# vLLM 0.33 ships CUDA 13.0 only; the scoring machine's driver (R570) runs CUDA <= 12.8 natively and 12.9 through
# minor-version compatibility, so the base is the newest cu129 release.
FROM vllm/vllm-openai:v0.32.0-cu129
# that image ships torchcodec 0.17.0+cu130 next to torch 2.13.0+cu129: it needs libcudart.so.13, so importing it raises
# OSError, which vLLM 0.32 does not catch (only ImportError/RuntimeError), and `vllm serve` dies before it starts
# (submission test 2026-11-15). Without it vLLM falls back as for a missing package; it serves only audio input.
RUN pip uninstall -y torchcodec
ENTRYPOINT []
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 \
    PIP_NO_CACHE_DIR=1 NCCL_P2P_DISABLE=1 PYTHONHASHSEED=0 \
    UV_PROJECT_ENVIRONMENT=/code/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /code
RUN pip install --no-cache-dir uv==0.11.29

# large and rarely changing first, so a code change rebuilds only the last layers
COPY models/ /code/models/
COPY training_code/ /code/training_code/
COPY code/pyproject.toml code/uv.lock /code/
RUN uv sync --frozen --no-dev --no-install-project --python python3
COPY code/ /code/
RUN uv sync --frozen --no-dev --python python3 \
    && /code/.venv/bin/datamate --help > /dev/null \
    && chmod +x /code/predict.sh

CMD ["bash", "/code/predict.sh"]
