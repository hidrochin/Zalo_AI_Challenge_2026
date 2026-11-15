"""Kaggle kernel (2x T4): serve the LLM with vLLM and answer the public test (optionally dev120 on train first).

Result for the leaderboard: /kaggle/working/out/public/submission.jsonl.

vLLM and the datamate package live in separate uv environments (their numpy/pandas pins conflict).
Everything lands in /kaggle/working/out (download with `kaggle kernels output`).
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

W = "/kaggle/working"
OUT = f"{W}/out"
PROJ = "/tmp/zalo"  # outside /kaggle/working so it is not saved as kernel output
ENV = "/tmp/vllm-env"  # stable vLLM
ENV_NIGHTLY = "/tmp/vllm-nightly"  # a vLLM nightly, for a model the release lacks (0.33.0 already serves Gemma 4)
PORT = 8000
# (model, vLLM env, extra args, thinking): the first one that starts and answers sanely is used. Gemma 4 12B first, as the
# final image will run it; T4 (sm_75) has no bf16 and no Marlin kernels, so a plain AWQ build in fp16, then the full model
GEMMA = ["--dtype", "half", "--reasoning-parser", "gemma4", "--limit-mm-per-prompt", '{"image":0,"audio":0,"video":0}']
MODELS = [
    # its config says compressed-tensors (W4A16), not awq: vLLM reads the method from the config
    ("nicklas373/gemma-4-12B-it-AWQ", ENV, GEMMA + ["--max-model-len", "32768"], True),
    ("google/gemma-4-12B-it", ENV, GEMMA + ["--max-model-len", "16384"], True),
    ("Qwen/Qwen3.5-9B", ENV, ["--dtype", "half", "--reasoning-parser", "qwen3", "--limit-mm-per-prompt", '{"image":0,"video":0}',
                              "--max-model-len", "32768"], True),
]
STAGES = os.environ.get("DM_STAGES", "public").split(",")  # probe | smoke | public | holdout | train_check | dev120 | train; push.sh sets it
ENGINE = os.environ.get("DM_ENGINE", "pandas")  # pandas | sql; push.sh sets it
ARGS = os.environ.get("DM_ARGS", "")  # extra `datamate run` flags (--catalog compiled --shop-views --judge unans); push.sh sets it
ONLY = os.environ.get("DM_MODEL", "")  # serve only this model (else the list in order); push.sh sets it
THINK = os.environ.get("DM_THINK", "")  # route: thinking only for the hard questions (--thinking route); push.sh sets it
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
        patch_attention(env)
        # a torchaudio built for another torch cannot load, and transformers imports it for any model with audio (Gemma 4):
        # without it, transformers skips audio
        sh(f"{env}/bin/python -c 'import torchaudio' 2>/dev/null || uv pip uninstall -q --python {env}/bin/python torchaudio",
           executable="/bin/bash")
        sh(f"set -o pipefail; {env}/bin/python -c 'import vllm, torch; print(vllm.__version__, torch.__version__)' | tee -a {OUT}/kaggle_run.txt",
           executable="/bin/bash")
        return True
    except subprocess.CalledProcessError as e:
        log(f"vLLM install in {env} failed: {e}")
        return False


def patch_attention(env):
    """T4 has 64 KB of shared memory per block. Gemma 4's full-attention layers have head size 512, and vLLM's Triton
    prefill tile of 32 KV rows then needs 96 KB ("OutOfResources: shared memory, Required: 98304"). A tile of 16 for
    heads of more than 512 bytes halves the K and V tiles, and one pipeline stage keeps a single copy of them; heads up to
    256 (Gemma's sliding layers, Qwen) keep vLLM's choice."""
    path = glob.glob(f"{env}/lib/python*/site-packages/vllm/v1/attention/ops/triton_unified_attention.py")
    edits = [("    if is_prefill:\n        return 32\n", "    if is_prefill:\n        return 16 if head_size * element_size > 512 else 32\n"),
             ("    launch_num_stages: int | None = None\n",
              "    launch_num_stages: int | None = 1 if head_size * q.element_size() > 512 else None\n")]
    src = open(path[0]).read() if path else ""
    for old, new in edits:
        state = "applied" if old in src else "already there" if new in src else "NOT FOUND"
        src = src.replace(old, new, 1)
        log(f"attention patch {new.split('=')[0].strip()}: {state}")
    if path:
        open(path[0], "w").write(src)


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
        vlog = f"{OUT}/vllm_{model.split('/')[-1]}.log"
        logf = open(vlog, "a")  # append: keep the log of a crashed server
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
        logf.flush()
        with open(vlog, errors="replace") as f:  # the reason, in the kernel's live log: the output files come only at the end
            lines = [ln for ln in f if "it/s]" not in ln]
        # the root cause sits in a worker's lines, above the API server's traceback
        errors = [ln for ln in lines if re.search(r"Error|ERROR|error:|not supported|Unsupported|out of memory|No kernel", ln)]
        log("vLLM errors:\n" + "".join(errors[-25:]) + "\nvLLM log tail:\n" + "".join(lines[-15:]))
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
        think = ("--thinking off" if not THINKING.get(model) else "--thinking route --max-tokens 2048" if THINK == "route"
                 else "--thinking on --max-tokens 8192")
        p = subprocess.run(f"cd {PROJ} && uv run --no-sync datamate run --base-url http://localhost:{PORT}/v1 --model llm "
                           f"--engine {ENGINE} {think} --concurrency 16 --exec-workers 4 --out {OUT}/{name} {args} {ARGS} 2>&1 "
                           f"| grep -v -e sitecustomize -e \"No module named 'wrapt'\" | tail -60",
                           shell=True, capture_output=True, text=True)
        print(p.stdout, flush=True)
        with open(f"{OUT}/{name}.txt", "a") as f:
            f.write(p.stdout)
        n = sum(1 for _ in open(sub)) if os.path.exists(sub) else 0
        log(f"stage {name}: {n}/{n_expected} answered after {time.time() - t0:.0f}s")
        # each shop's slot fills with their triggers, model replies and checks (the run dir keeps only the applied ones)
        subprocess.run(f"mkdir -p {OUT}/slots && cp {PROJ}/cache/ingest/*/*.slots.json {OUT}/slots/ 2>/dev/null", shell=True)
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


def smoke_ids(split, path, per_shop=2):
    """The first questions of every shop: each shop's compile, shop views and setup run once."""
    seen, ids = {}, []
    for ln in open(f"{PROJ}/data/{split}/questions.jsonl"):
        q = json.loads(ln)
        if seen.get(q["business_id"], 0) < per_shop:
            seen[q["business_id"]] = seen.get(q["business_id"], 0) + 1
            ids.append(q["id"])
    open(path, "w").write("\n".join(ids) + "\n")
    return len(ids)


def smoke_ok(name, n_expected):
    """Every question has a line, no traceback, the model's replies parse and some answers come out."""
    sub = f"{OUT}/{name}/submission.jsonl"
    rows = [json.loads(ln) for ln in open(sub)] if os.path.exists(sub) else []
    answered = sum(1 for r in rows if r.get("type") not in ("abstain", None))
    llm_err = 0
    for f in glob.glob(f"{OUT}/{name}/logs/*.json"):
        samples = json.load(open(f)).get("samples") or []
        llm_err += bool(samples) and all(str(s.get("error") or "").startswith("LLM error") for s in samples)
    stdout = open(f"{OUT}/{name}.txt").read() if os.path.exists(f"{OUT}/{name}.txt") else ""
    checks = {"all lines": len(rows) >= n_expected, "no traceback": "Traceback" not in stdout,
              "LLM errors < 25%": llm_err < 0.25 * n_expected, "answered >= 25%": answered >= 0.25 * n_expected}
    log(f"smoke: {len(rows)}/{n_expected} lines, {answered} answered, {llm_err} with only LLM errors; " +
        ", ".join(f"{k}: {'ok' if v else 'FAIL'}" for k, v in checks.items()))
    return all(checks.values())


def main():
    setup()
    model, proc = start_server(ONLY or None)
    log(f"using {model} (thinking {'on' if THINKING.get(model) else 'off'}); run flags: {ARGS or '-'}")
    if "probe" in STAGES:  # does the model serve and think: one request, no questions
        t0 = time.time()
        r = _get("/v1/chat/completions", {"model": "llm", "temperature": 0, "max_tokens": 1024,
                                           "messages": [{"role": "user", "content": "Một cửa hàng bán 3 ly giá 25.000đ và 2 ly giá "
                                                         "30.000đ. Doanh thu là bao nhiêu? Trả lời một con số."}],
                                           "chat_template_kwargs": {"enable_thinking": True}}, timeout=600)
        msg = r["choices"][0]["message"]
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        log(f"probe: {time.time() - t0:.0f}s, {r.get('usage')}, reasoning {len(reasoning)} chars, answer {msg.get('content')!r}")
    if "holdout" in STAGES:  # held-out train shops (assets/holdout.txt): never used to pick prompts or fixes
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split train --workers 4 | tail -5")
        n = sum(1 for ln in open(f"{PROJ}/assets/holdout.txt") if ln.strip())
        proc = run_stage("holdout", "--split train --ids-file assets/holdout.txt", n, model, proc)
    if "train_check" in STAGES:  # 106 train ids (lint and table cases + controls): no right answer may turn wrong
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split train --workers 4 | tail -5")
        n = sum(1 for ln in open(f"{PROJ}/assets/train_check.txt") if ln.strip())
        proc = run_stage("train_check", "--split train --ids-file assets/train_check.txt", n, model, proc)
    if "dev120" in STAGES:
        n = sum(1 for ln in open(f"{PROJ}/assets/dev120.txt") if ln.strip())
        proc = run_stage("dev120", "--split train --ids-file assets/dev120.txt", n, model, proc)
    if "train" in STAGES:
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split train --workers 4 | tail -20")
        sales_lines_report("train")
        n = sum(1 for _ in open(f"{PROJ}/data/train/questions.jsonl"))
        proc = run_stage("train", "--split train", n, model, proc)
    if "smoke" in STAGES:  # a few questions of every shop end to end (compile, shop views, judge) before the long run
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split public_test --workers 4 | tail -20")
        n = smoke_ids("public_test", f"{OUT}/smoke_ids.txt")
        proc = run_stage("smoke", f"--split public_test --ids-file {OUT}/smoke_ids.txt", n, model, proc, max_restarts=0)
        if not smoke_ok("smoke", n):
            proc.terminate()
            sys.exit("smoke run failed: the long stages were not started")
    if "public" in STAGES:
        sh(f"cd {PROJ} && uv run --no-sync datamate ingest --split public_test --workers 4 | tail -20")
        sales_lines_report("public_test")
        n = sum(1 for _ in open(f"{PROJ}/data/public_test/questions.jsonl"))
        proc = run_stage("public", "--split public_test", n, model, proc)
        sh(f"cp {OUT}/public/submission.jsonl {W}/submission.jsonl && wc -l {W}/submission.jsonl")
    proc.terminate()


if __name__ == "__main__":
    main()
