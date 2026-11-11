"""Kaggle kernel (2x T4): serve the LLM with vLLM and answer the public test (optionally dev120 on train first).

Result for the leaderboard: /kaggle/working/out/public/submission.jsonl.

vLLM and the datamate package live in separate uv environments (their numpy/pandas pins conflict).
Everything lands in /kaggle/working/out (download with `kaggle kernels output`).
"""

import glob
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

W = "/kaggle/working"
OUT = f"{W}/out"
PROJ = "/tmp/zalo"  # outside /kaggle/working so it is not saved as kernel output
ENV = "/tmp/vllm-env"  # stable vLLM
ENV_NIGHTLY = "/tmp/vllm-nightly"  # Gemma 4 12B needs a vLLM nightly (2026-11); installed only when a Gemma model is tried
PORT = 8000
# (model, vLLM env, extra args, thinking): the first one that starts and answers sanely is used. Gemma 4 12B first, as the
# final image will run it; T4 (sm_75) has no bf16 and no Marlin kernels, so a plain AWQ build in fp16, then the full model
GEMMA = ["--dtype", "half", "--reasoning-parser", "gemma4"]
MODELS = [
    ("nicklas373/gemma-4-12B-it-AWQ", ENV_NIGHTLY, GEMMA + ["--quantization", "awq", "--max-model-len", "32768"], True),
    ("google/gemma-4-12B-it", ENV_NIGHTLY, GEMMA + ["--max-model-len", "16384"], True),
    ("Qwen/Qwen3.5-9B", ENV, ["--dtype", "half", "--limit-mm-per-prompt", '{"image":0,"video":0}', "--max-model-len", "32768"], True),
]
STAGES = os.environ.get("DM_STAGES", "public").split(",")  # public | holdout | dev120 | dev120_oracle | train; push.sh sets it
ENGINE = os.environ.get("DM_ENGINE", "pandas")  # pandas | sql; push.sh sets it
ARGS = os.environ.get("DM_ARGS", "")  # extra `datamate run` flags (--catalog compiled --shop-views --judge unans); push.sh sets it
ONLY = os.environ.get("DM_MODEL", "")  # serve only this model (else the list in order); push.sh sets it
BUILD = os.environ.get("DM_BUILD", "")  # sha1 of assets/BUILD.txt at push time; push.sh sets it
THINKING = {}  # model -> thinking on, filled from MODELS


def sh(cmd, **kw):
    print(f"$ {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, check=True, **kw)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    with open(f"{OUT}/kaggle_run.txt", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def setup():
    os.makedirs(OUT, exist_ok=True)
    sh("ls -R /kaggle/input | head -20")
    bundle = glob.glob("/kaggle/input/**/bundle.tar.gz", recursive=True)
    roots = sorted({os.path.dirname(p) for p in glob.glob("/kaggle/input/**/pyproject.toml", recursive=True)}, key=len)
    data = [r for r in roots if os.path.isdir(f"{r}/data/train")]
    code = [r for r in roots if not os.path.isdir(f"{r}/data")]
    # Kaggle unpacks archives in datasets. push.sh with DM_DATA mounts two: an older one for data/, a code-only one
    if data:
        sh(f"cp -r {data[0]} {PROJ}")
    if bundle or code:  # the current code over the data: drop the older code first
        sh(f"mkdir -p {PROJ} && rm -rf {PROJ}/src {PROJ}/assets {PROJ}/pyproject.toml {PROJ}/uv.lock")
    if bundle:
        sh(f"tar -xzf {bundle[0]} -C {PROJ}")
    elif code:
        sh(f"cp -r {code[0]}/src {code[0]}/assets {code[0]}/pyproject.toml {code[0]}/uv.lock {PROJ}/")
    elif not data:
        sh(f"cp -r {roots[0]} {PROJ}")
    sh(f"find {PROJ} -name '._*' -delete && ls {PROJ} {PROJ}/data")
    sh(f"cp {PROJ}/assets/BUILD.txt {OUT}/ || true")
    if BUILD:  # a stale dataset version mounts old code: stop before hours of GPU time
        got = hashlib.sha1(open(f"{PROJ}/assets/BUILD.txt", "rb").read()).hexdigest()
        if got != BUILD:
            log(f"mounted build {got} is not the pushed build {BUILD}; stopping")
            sys.exit(1)
    sh("pip install -q uv")
    sh(f"cd {PROJ} && uv sync --frozen --no-dev -q")
    sh("nvidia-smi | tee -a " + f"{OUT}/kaggle_run.txt")


def install(env):
    """The vLLM env a model needs, once; False when it cannot be installed."""
    if os.path.exists(f"{env}/bin/vllm"):
        return True
    pip = f"uv venv -q --python 3.12 {env} && uv pip install -q --python {env}/bin/python"
    cmd = (f"{pip} -U vllm --pre --extra-index-url https://wheels.vllm.ai/nightly/cu129 "
           "--extra-index-url https://download.pytorch.org/whl/cu129 --index-strategy unsafe-best-match"
           if env == ENV_NIGHTLY else f"{pip} vllm")
    try:
        sh(cmd)
        sh(f"set -o pipefail; {env}/bin/python -c 'import vllm, torch; print(vllm.__version__, torch.__version__)' | tee -a {OUT}/kaggle_run.txt",
           executable="/bin/bash")
        return True
    except subprocess.CalledProcessError as e:
        log(f"vLLM install in {env} failed: {e}")
        return False


def _get(path, data=None, timeout=10):
    req = urllib.request.Request(f"http://localhost:{PORT}{path}", data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}") if path != "/health" else {}


def start_server(only=None):
    for model, env, extra, thinking in MODELS:
        if only and model != only:
            continue
        if not install(env):
            continue
        THINKING[model] = thinking
        log(f"starting vLLM with {model}")
        args = [f"{env}/bin/vllm", "serve", model, "--served-model-name", "llm", "--port", str(PORT),
                "--tensor-parallel-size", "2", "--gpu-memory-utilization", "0.92",
                "--max-num-seqs", "16", "--enable-prefix-caching", "--seed", "0", *extra]
        logf = open(f"{OUT}/vllm_{model.split('/')[-1]}.log", "a")  # append: keep the log of a crashed server
        proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)
        t0 = time.time()
        while proc.poll() is None and time.time() - t0 < 1800:
            try:
                _get("/health")
                break
            except Exception:
                time.sleep(10)
        if proc.poll() is None:
            try:
                r = _get("/v1/chat/completions", {"model": "llm", "temperature": 0, "max_tokens": 20,
                                                   "messages": [{"role": "user", "content": "What is 17+25? Reply with the number only."}],
                                                   "chat_template_kwargs": {"enable_thinking": False}}, timeout=300)
                text = r["choices"][0]["message"]["content"] or ""
                log(f"{model} up after {time.time() - t0:.0f}s; sanity reply: {text!r}")
                if "42" in text:
                    return model, proc
            except Exception as e:
                log(f"{model} sanity request failed: {e!r}")
        else:
            log(f"{model} server exited with code {proc.returncode}")
        proc.kill()
        proc.wait()
        time.sleep(15)
    sys.exit("no model could be served")


def server_ok(proc) -> bool:
    if proc.poll() is not None:
        return False
    try:
        _get("/health")
        return True
    except Exception:
        return False


def log_host_state(why):
    """Free memory, GPU state and the kernel's OOM-killer lines, to explain a dead model server."""
    log(f"diagnostics ({why})")
    subprocess.run(f"(free -g; nvidia-smi; dmesg 2>/dev/null | grep -iE 'killed process|out of memory' | tail -5) "
                   f">> {OUT}/kaggle_run.txt 2>&1", shell=True)


def run_stage(name, args, n_expected, model, proc, max_restarts=3):
    """Run until every question has a line; if the model server dies, restart it and resume the run."""
    sub = f"{OUT}/{name}/submission.jsonl"
    for attempt in range(1 + max_restarts):
        log(f"stage {name}: start (attempt {attempt + 1})")
        t0 = time.time()
        think = "--thinking on --max-tokens 8192" if THINKING.get(model) else "--thinking off"
        p = subprocess.run(f"cd {PROJ} && uv run --no-sync datamate run --base-url http://localhost:{PORT}/v1 --model llm "
                           f"--engine {ENGINE} {think} --concurrency 16 --exec-workers 4 --out {OUT}/{name} {args} {ARGS} 2>&1 "
                           f"| grep -v -e sitecustomize -e \"No module named 'wrapt'\" | tail -60",
                           shell=True, capture_output=True, text=True)
        print(p.stdout, flush=True)
        with open(f"{OUT}/{name}.txt", "a") as f:
            f.write(p.stdout)
        n = sum(1 for _ in open(sub)) if os.path.exists(sub) else 0
        log(f"stage {name}: {n}/{n_expected} answered after {time.time() - t0:.0f}s")
        if n >= n_expected:
            return proc
        log_host_state(f"stage {name} stopped early")
        if not server_ok(proc):
            log("model server is down; restarting it")
            proc.kill()
            proc.wait()
            time.sleep(15)
            _, proc = start_server(only=model)  # same model, so all answers come from one model
    log(f"stage {name}: incomplete after {max_restarts} restarts")
    return proc


def sales_lines_report(split):
    """Which businesses get a sales_lines() table: a quick check that the bundle ingests as it does locally."""
    code = ("from pathlib import Path\nfrom datamate.canon import build_sales\nfrom datamate.ingest import load_business\n"
            f"d=Path('data/{split}')\n"
            "for b in sorted(p.name for p in (d/'data').iterdir() if p.is_dir()):\n"
            "    biz=load_business(d,b,Path('cache/ingest'))\n"
            "    s,_=build_sales(biz.tables,{n:t.roles for n,t in biz.info.items()})\n"
            "    print(b,'sales_lines' if s is not None else 'NO sales_lines')\n")
    p = subprocess.run(["uv", "run", "--no-sync", "python", "-c", code], cwd=PROJ, capture_output=True, text=True)
    log("sales_lines per business:\n" + p.stdout + p.stderr[-500:])


def main():
    setup()
    model, proc = start_server(ONLY or None)
    log(f"using {model} (thinking {'on' if THINKING.get(model) else 'off'}); run flags: {ARGS or '-'}")
    if "holdout" in STAGES:  # held-out train shops (assets/holdout.txt): never used to pick prompts or fixes
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split train --workers 4 | tail -5")
        n = sum(1 for ln in open(f"{PROJ}/assets/holdout.txt") if ln.strip())
        proc = run_stage("holdout", "--split train --ids-file assets/holdout.txt", n, model, proc)
    if "dev120" in STAGES:
        n = sum(1 for ln in open(f"{PROJ}/assets/dev120.txt") if ln.strip())
        proc = run_stage("dev120", "--split train --ids-file assets/dev120.txt", n, model, proc)
    if "dev120_oracle" in STAGES:  # table-selection experiment: only the tables a right answer used
        n = sum(1 for ln in open(f"{PROJ}/assets/dev120.txt") if ln.strip())
        proc = run_stage("dev120_oracle", "--split train --ids-file assets/dev120.txt --keep-tables assets/oracle_tables.json", n, model, proc)
    if "train" in STAGES:
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split train --workers 4 | tail -20")
        sales_lines_report("train")
        n = sum(1 for _ in open(f"{PROJ}/data/train/questions.jsonl"))
        proc = run_stage("train", "--split train", n, model, proc)
    if "public" in STAGES:
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split public_test --workers 4 | tail -20")
        sales_lines_report("public_test")
        n = sum(1 for _ in open(f"{PROJ}/data/public_test/questions.jsonl"))
        proc = run_stage("public", "--split public_test", n, model, proc)
        sh(f"cp {OUT}/public/submission.jsonl {W}/submission.jsonl && wc -l {W}/submission.jsonl")
    if "oracle" in STAGES:  # interventions on the open public errors (debug/oracle/build.py); base = same questions, no note
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split public_test --workers 4 | tail -20")
        for arm in ("base", "fact", "table", "rule"):
            ids = "assets/oracle/ids.txt" if arm == "base" else f"assets/oracle/ids_{arm}.txt"
            n = sum(1 for ln in open(f"{PROJ}/{ids}") if ln.strip())
            extra = "" if arm == "base" else f" --oracle assets/oracle/{arm}.json"
            proc = run_stage(f"oracle_{arm}", f"--split public_test --ids-file {ids}{extra}", n, model, proc)
    proc.terminate()


if __name__ == "__main__":
    main()
