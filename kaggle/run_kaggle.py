"""Kaggle kernel (2x T4): serve the LLM with vLLM and answer the public test (optionally dev120 on train first).

Result for the leaderboard: /kaggle/working/out/public/submission.jsonl.

vLLM and the datamate package live in separate uv environments (their numpy/pandas pins conflict).
Everything lands in /kaggle/working/out (download with `kaggle kernels output`).
"""

import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

W = "/kaggle/working"
OUT = f"{W}/out"
PROJ = "/tmp/zalo"  # outside /kaggle/working so it is not saved as kernel output
ENV = "/tmp/vllm-env"
PORT = 8000
MODELS = [  # first one that starts and answers sanely is used
    ("Qwen/Qwen3.5-9B", ["--dtype", "half", "--limit-mm-per-prompt", '{"image":0,"video":0}']),
    ("Qwen/Qwen3-14B-AWQ", ["--dtype", "half", "--quantization", "awq"]),
]
STAGES = os.environ.get("DM_STAGES", "public").split(",")  # public | dev120 | train (all 3000, scored); push.sh sets it
ENGINE = os.environ.get("DM_ENGINE", "pandas")  # pandas | sql; push.sh sets it


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
    if bundle:
        sh(f"mkdir -p {PROJ} && tar -xzf {bundle[0]} -C {PROJ}")
    else:  # Kaggle unpacks archives in datasets
        root = os.path.dirname(min(glob.glob("/kaggle/input/**/pyproject.toml", recursive=True), key=len))
        sh(f"cp -r {root} {PROJ} && find {PROJ} -name '._*' -delete")
    sh(f"cp {PROJ}/BUILD.txt {OUT}/ && cat {OUT}/BUILD.txt")
    sh("pip install -q uv")
    sh(f"cd {PROJ} && uv sync --frozen --no-dev -q")
    sh(f"uv venv -q --python 3.12 {ENV} && uv pip install -q --python {ENV}/bin/python vllm")
    sh(f"set -o pipefail; {ENV}/bin/python -c 'import vllm, torch; print(vllm.__version__, torch.__version__)' | tee -a {OUT}/kaggle_run.txt",
       executable="/bin/bash")
    sh("nvidia-smi | tee -a " + f"{OUT}/kaggle_run.txt")


def _get(path, data=None, timeout=10):
    req = urllib.request.Request(f"http://localhost:{PORT}{path}", data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}") if path != "/health" else {}


def start_server(only=None):
    for model, extra in MODELS:
        if only and model != only:
            continue
        log(f"starting vLLM with {model}")
        args = [f"{ENV}/bin/vllm", "serve", model, "--served-model-name", "qwen", "--port", str(PORT),
                "--tensor-parallel-size", "2", "--max-model-len", "24576", "--gpu-memory-utilization", "0.92",
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
                r = _get("/v1/chat/completions", {"model": "qwen", "temperature": 0, "max_tokens": 20,
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
        p = subprocess.run(f"cd {PROJ} && uv run --no-sync datamate run --base-url http://localhost:{PORT}/v1 --model qwen "
                           f"--engine {ENGINE} --thinking off --concurrency 16 --exec-workers 4 --out {OUT}/{name} {args} 2>&1 "
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
    model, proc = start_server()
    log(f"using {model}")
    if "dev120" in STAGES:
        n = sum(1 for ln in open(f"{PROJ}/assets/dev120.txt") if ln.strip())
        proc = run_stage("dev120", "--split train --ids-file assets/dev120.txt", n, model, proc)
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
    proc.terminate()


if __name__ == "__main__":
    main()
