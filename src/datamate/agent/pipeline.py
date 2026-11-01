"""End-to-end run: questions -> prompt -> LLM samples -> sandbox -> vote -> submission + logs (+ score)."""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tqdm import tqdm

from datamate.agent.hints import time_hints
from datamate.agent.prompt import build_system, business_part, check_message, extract_code, question_part, repair_message
from datamate.agent.retrieve import ExampleBank
from datamate.agent.sandbox import ExecPool
from datamate.agent.vote import decide
from datamate.canon import build_sales
from datamate.eval.scorer import read_jsonl, score
from datamate.ingest import load_business, load_calendar
from datamate.ingest.schema_card import schema_card


@dataclass
class RunConfig:
    split: str = "train"
    data_root: Path = Path("data")
    out_dir: Path = Path("outputs/run")
    cache_dir: Path = Path("cache/ingest")
    backend: str = "local"  # local | claude | dummy
    model: str = "qwen3.5:9b"
    base_url: str = "http://localhost:11434/v1"
    samples: int = 1
    temperature: float = 0.6
    max_tokens: int = 2048
    enable_thinking: bool | None = None
    effort: str = "medium"
    concurrency: int = 4
    exec_workers: int = 2
    timeout: int = 30
    repair: int = 1
    k_examples: int = 4
    examples_path: Path | None = Path("assets/examples.jsonl")
    min_share: float = 0.5
    unans_share: float = 0.6
    seed: int = 0
    limit: int | None = None
    businesses: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    resume: bool = True


class DummyLLM:
    """Plumbing test without a model: answers the row count of the first table."""

    def generate(self, system, user_parts, history, temperature, seed) -> str:
        return "```python\nanswer_number(len(next(iter(T.values()))), kind='count')\n```"


def make_llm(cfg: RunConfig):
    if cfg.backend == "dummy":
        return DummyLLM()
    if cfg.backend == "claude":
        if cfg.split != "train":
            raise SystemExit("The Claude backend is development-only and may only be run on the train split (rules 3, 6).")
        from datamate.agent.llm_claude import ClaudeLLM

        return ClaudeLLM(model=cfg.model, effort=cfg.effort, max_tokens=max(cfg.max_tokens, 16000))
    from datamate.agent.llm_local import LocalLLM

    return LocalLLM(cfg.base_url, cfg.model, max_tokens=cfg.max_tokens, enable_thinking=cfg.enable_thinking)


def _select(questions: list[dict], cfg: RunConfig) -> list[dict]:
    qs = questions
    if cfg.ids:
        wanted = set(cfg.ids)
        qs = [q for q in qs if q["id"] in wanted]
    if cfg.businesses:
        wanted = set(cfg.businesses)
        qs = [q for q in qs if q["business_id"] in wanted]
    if cfg.limit is not None and cfg.limit < len(qs):
        qs = random.Random(cfg.seed).sample(qs, cfg.limit)
    return qs


def solve_question(q: dict, llm, pool: ExecPool, system: str, biz_part: str, bank: ExampleBank, cfg: RunConfig,
                   cal=None) -> dict:
    t0 = time.time()
    examples = bank.query(q["question"], exclude_business=q["business_id"], k=cfg.k_examples)
    hints = time_hints(q["question"], q["as_of_date"], cal)
    user_parts = [biz_part, question_part(q["question"], q["as_of_date"], examples, hints)]
    samples = []
    for s in range(cfg.samples):
        temperature = 0.0 if s == 0 else cfg.temperature
        history: list[dict] = []
        sample = {"seed": cfg.seed + s, "temperature": temperature, "attempts": []}
        out = {"result": None, "error": "not run", "stdout": ""}
        for attempt in range(1 + cfg.repair):
            try:
                text = llm.generate(system, user_parts, history, temperature=temperature, seed=cfg.seed + s)
            except Exception as e:  # network/model errors: record and stop this sample
                out = {"result": None, "error": f"LLM error: {e!r}", "stdout": ""}
                sample["attempts"].append({"llm_error": repr(e)})
                break
            code = extract_code(text)
            if code is None:
                out = {"result": None, "error": "No ```python code block found in the reply.", "stdout": ""}
            else:
                out = pool.run(code, q["as_of_date"])
            sample["attempts"].append({"reply": text, "code": code, **out})
            if out["error"] is None and not (out.get("warning") and attempt < cfg.repair):
                break
            msg = repair_message(out) if out["error"] else check_message(out["warning"])
            history += [{"role": "assistant", "content": text}, {"role": "user", "content": msg}]
        flagged = next((a["result"] for a in sample["attempts"] if a.get("warning") and a.get("result")), None)
        if out["result"] is None and flagged is not None:  # the re-check broke a program that only had a soft warning
            out = {**out, "result": flagged, "error": None}
        sample["result"], sample["error"] = out["result"], out["error"]
        samples.append(sample)
    final, vote = decide([s["result"] for s in samples], cfg.min_share, cfg.unans_share)
    return {
        "id": q["id"], "business_id": q["business_id"], "question": q["question"], "as_of_date": q["as_of_date"],
        "final": final, "vote": vote, "examples": [e["id"] for e in examples], "hints": hints, "samples": samples,
        "elapsed": round(time.time() - t0, 2),
    }


def run(cfg: RunConfig) -> dict:
    split_dir = Path(cfg.data_root) / cfg.split
    out_dir = Path(cfg.out_dir)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(json.dumps(asdict(cfg), default=str, indent=2), encoding="utf-8")

    questions = _select(read_jsonl(split_dir / "questions.jsonl"), cfg)
    sub_path = out_dir / "submission.jsonl"
    done = set()
    if cfg.resume and sub_path.exists():
        done = {json.loads(line)["id"] for line in sub_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo = [q for q in questions if q["id"] not in done]

    llm = make_llm(cfg)
    system = build_system((split_dir / "rulebook.md").read_text(encoding="utf-8"))
    bank = ExampleBank.from_train(Path(cfg.data_root) / "train", cfg.examples_path)
    cal = load_calendar(split_dir)
    lock = threading.Lock()
    t_start = time.time()

    by_biz: dict[str, list[dict]] = {}
    for q in todo:
        by_biz.setdefault(q["business_id"], []).append(q)

    with sub_path.open("a", encoding="utf-8") as sub, tqdm(total=len(todo), desc="questions") as bar:
        for biz_id in sorted(by_biz):
            biz = load_business(split_dir, biz_id, cfg.cache_dir)
            _, sales_desc = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
            biz_part = business_part(biz_id, biz.readme.text, schema_card(biz) + "\n\n## " + sales_desc)
            pool = ExecPool(split_dir, biz_id, cfg.cache_dir, n=cfg.exec_workers, timeout=cfg.timeout)
            try:
                with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
                    futs = [ex.submit(solve_question, q, llm, pool, system, biz_part, bank, cfg, cal) for q in by_biz[biz_id]]
                    for f in as_completed(futs):
                        rec = f.result()
                        with lock:
                            final = {k: v for k, v in rec["final"].items() if not k.startswith("_")}
                            sub.write(json.dumps({"id": rec["id"], **final}, ensure_ascii=False) + "\n")
                            sub.flush()
                            (out_dir / "logs" / f"{rec['id']}.json").write_text(
                                json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
                        bar.update(1)
            finally:
                pool.close()

    summary = {"questions": len(questions), "new": len(todo), "seconds": round(time.time() - t_start, 1)}
    ans_path = split_dir / "answers.jsonl"
    if ans_path.exists():
        summary["score"] = score_run(out_dir, split_dir, [q["id"] for q in questions])
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def score_run(out_dir: Path, split_dir: Path, ids: list[str] | None = None) -> dict:
    preds = {p["id"]: p for p in read_jsonl(Path(out_dir) / "submission.jsonl")}
    refs = {r["id"]: r for r in read_jsonl(Path(split_dir) / "answers.jsonl")}
    meta = {q["id"]: q for q in read_jsonl(Path(split_dir) / "questions.jsonl")}
    ids = [i for i in (ids or list(preds)) if i in refs]
    rep = score(preds, refs, meta, ids)
    (Path(out_dir) / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: v for k, v in rep.items() if k != "per_question"}


def export_examples(run_dirs: list[Path], split_dir: Path, out_path: Path) -> int:
    """Append programs whose final answer scored correct to the example bank (train only)."""
    refs = {r["id"]: r for r in read_jsonl(Path(split_dir) / "answers.jsonl")}
    from datamate.agent.vote import answer_key
    from datamate.eval.scorer import is_correct

    existing = {}
    if Path(out_path).exists():
        existing = {e["id"]: e for e in read_jsonl(out_path)}
    added = 0
    for rd in run_dirs:
        for log in sorted((Path(rd) / "logs").glob("*.json")):
            rec = json.loads(log.read_text(encoding="utf-8"))
            ref = refs.get(rec["id"])
            final = rec["final"]
            if ref is None or final.get("type") == "abstain" or not is_correct(final, ref):
                continue
            key = answer_key(final)
            for s in rec["samples"]:
                if s["result"] is not None and answer_key(s["result"]) == key:
                    code = next(a["code"] for a in reversed(s["attempts"]) if a.get("code"))
                    if rec["id"] not in existing:
                        added += 1
                    existing[rec["id"]] = {"id": rec["id"], "business_id": rec["business_id"], "code": code}
                    break
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(out_path).open("w", encoding="utf-8") as f:
        for e in existing.values():
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return added
