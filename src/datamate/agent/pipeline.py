"""End-to-end run: questions -> preprocess -> prompt -> LLM samples -> sandbox (+ answer checks) -> vote
-> postprocess -> submission + logs (+ score)."""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import NamedTuple

from tqdm import tqdm

from datamate.agent.hints import time_hints
from datamate.agent.prompt import build_system, business_part, check_message, extract_code, question_part, repair_message
from datamate.agent.retrieve import ExampleBank
from datamate.agent.sandbox import ExecPool, name_index
from datamate.agent.vote import decide
from datamate.canon import build_sales
from datamate.eval.scorer import read_jsonl, score
from datamate.ingest import load_business, load_calendar
from datamate.ingest.schema_card import schema_card
from datamate.postprocess import Expect, check_answer, date_window, expect_answer, finalize, invariants
from datamate.preprocess import EntityIndex, rules_normalize
from datamate.preprocess.rewrite import names_block, rewrite
from datamate.sql.agent import (SqlPool, build_sql_system, extract_sql, sql_card, sql_check_message, sql_question_part,
                                sql_repair_message, sql_shadow)
from datamate.sql.store import STORE_VERSION, ensure_store


@dataclass
class RunConfig:
    split: str = "train"
    data_root: Path = Path("data")
    out_dir: Path = Path("outputs/run")
    cache_dir: Path = Path("cache/ingest")
    backend: str = "local"  # local | claude | gemini | dummy
    engine: str = "pandas"  # pandas | sql: what the model writes
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
    llm_retries: int = 6  # server errors: back off 5..120 s, then stop the run (a resumed run retries)
    normalize: str = "llm"  # off | rules | llm: question preprocessing (rules, then one LLM rewrite)
    rewrite_max_tokens: int = 400
    seed: int = 0
    limit: int | None = None
    businesses: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    resume: bool = True


def _pandas_shadow(text: str, hints: list[str]) -> tuple[str, str] | None:
    calls = [h.split(" -> ")[1].split(" = ")[0].split("  #")[0] for h in hints]
    return (text, calls[0]) if len(calls) == 1 else None  # semantic cross-check needs exactly one period


class Engine(NamedTuple):
    """What differs between answering with a pandas program and with a SQL query."""
    build_system: Callable
    extract: Callable
    question_part: Callable
    shadow: Callable
    repair_message: Callable
    check_message: Callable
    no_code: str


ENGINES = {
    "pandas": Engine(build_system, extract_code, question_part, _pandas_shadow, repair_message, check_message,
                     "No ```python code block found in the reply."),
    "sql": Engine(build_sql_system, extract_sql, sql_question_part, sql_shadow, sql_repair_message, sql_check_message,
                  "No ```sql block or UNANSWERABLE line found in the reply."),
}


class LLMUnavailable(RuntimeError):
    """The model server kept failing. The run stops without writing these questions, so a resumed run answers them."""


_LLM_DOWN = threading.Event()  # set once one thread gives up, so the other threads stop too


def _generate(llm, cfg: "RunConfig", *args, **kw) -> str:
    """llm.generate with backoff on connection/5xx errors; 4xx (e.g. prompt too long) is the question's own error."""
    delay = 5
    for attempt in range(cfg.llm_retries + 1):
        if _LLM_DOWN.is_set():
            raise LLMUnavailable("model server is down")
        try:
            return llm.generate(*args, **kw)
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt == cfg.llm_retries:
                _LLM_DOWN.set()
                raise LLMUnavailable(repr(e)) from e
            print(f"LLM error ({e!r}); retry in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise AssertionError("unreachable")


class DummyLLM:
    """Plumbing test without a model: answers the row count of the first table."""

    def generate(self, system, user_parts, history, temperature, seed, max_tokens=None) -> str:
        if "DuckDB" in system:
            return "```sql\n-- type: count\nselect count(*) from calendar\n```"
        return "```python\nanswer_number(len(next(iter(T.values()))), kind='count')\n```"


def make_llm(cfg: RunConfig):
    if cfg.backend == "dummy":
        return DummyLLM()
    if cfg.backend == "claude":
        if cfg.split != "train":
            raise SystemExit("The Claude backend is development-only and may only be run on the train split (rules 3, 6).")
        from datamate.agent.llm_claude import ClaudeLLM

        return ClaudeLLM(model=cfg.model, effort=cfg.effort, max_tokens=max(cfg.max_tokens, 16000))
    if cfg.backend == "gemini":
        if cfg.split != "train":
            raise SystemExit("The Gemini backend runs on the train split only; public test labels are on hold.")
        from datamate.agent.llm_gemini import GeminiLLM

        return GeminiLLM(cfg.model)
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


def preprocess(q: dict, llm, cfg: RunConfig, index: EntityIndex | None) -> tuple[str, dict | None, dict]:
    """(rule-cleaned text, validated LLM rewrite or None, log record) for one question."""
    if cfg.normalize == "off":
        return q["question"], None, {}
    rules = rules_normalize(q["question"], index)
    rec = {"rules": rules.text, "changes": rules.changes, "amounts": rules.amounts,
           "mentions": [{"kind": m.kind, "name": m.name, "full": m.full} for m in rules.mentions]}
    rw = None
    if cfg.normalize == "llm":
        def gen(system, parts):
            return _generate(llm, cfg, system, parts, [], temperature=0.0, seed=cfg.seed, max_tokens=cfg.rewrite_max_tokens)

        try:
            rw, reply = rewrite(gen, q["question"], rules, index.vocab if index is not None else frozenset())
        except LLMUnavailable:
            raise
        except Exception as e:  # a rejected request only loses the rewrite
            reply = f"LLM error: {e!r}"
        rec.update(rewrite=rw, rewrite_reply=reply)
    rec["norm"] = {
        "normalized": (rw or {}).get("normalized") or rules.text,
        "sub_questions": (rw or {}).get("sub_questions") or [],
        "names": names_block(rules).splitlines()[1:],
        "amounts": rules.amounts,
    }
    return rules.text, rw, rec


def _expect_text(e: Expect) -> str | None:
    if e.type is None:
        return None
    t = {"date": "date 'YYYY-MM' (a month)" if e.month else "date 'YYYY-MM-DD' (a day)" if e.month is False else "date"}.get(e.type, e.type)
    return t + (f", at most {e.k} items" if e.k else "")


def solve_question(q: dict, llm, pool: ExecPool | SqlPool, system: str, biz_part: str, bank: ExampleBank, cfg: RunConfig,
                   cal=None, index: EntityIndex | None = None) -> dict:
    t0 = time.time()
    engine = ENGINES[cfg.engine]
    text, rw, pre = preprocess(q, llm, cfg, index)
    expect = expect_answer(text, rw)
    norm = {**pre.get("norm", {}), "expect": _expect_text(expect) if expect.hard else None}
    examples = bank.query(q["question"], exclude_business=q["business_id"], k=cfg.k_examples)
    hints = time_hints(text, q["as_of_date"], cal)
    expect.window = date_window(text, hints)
    shadow = engine.shadow(text, hints)
    user_parts = [biz_part, engine.question_part(q["question"], q["as_of_date"], examples, hints, norm)]
    samples = []
    for s in range(cfg.samples):
        temperature = 0.0 if s == 0 else cfg.temperature
        history: list[dict] = []
        sample = {"seed": cfg.seed + s, "temperature": temperature, "attempts": []}
        out = {"result": None, "error": "not run", "stdout": ""}
        for attempt in range(1 + cfg.repair):
            try:
                text_reply = _generate(llm, cfg, system, user_parts, history, temperature=temperature, seed=cfg.seed + s)
            except LLMUnavailable:
                raise
            except Exception as e:  # request rejected by the model server: record and stop this sample
                out = {"result": None, "error": f"LLM error: {e!r}", "stdout": ""}
                sample["attempts"].append({"llm_error": repr(e)})
                break
            code = engine.extract(text_reply)
            if code is None:
                out = {"result": None, "error": engine.no_code, "stdout": ""}
            else:
                out = pool.run(code, q["as_of_date"], shadow)
            if out["error"] is None and out["result"] is not None:
                why, hard = check_answer(out["result"], expect)
                broken = invariants(out["result"], text, expect.window)  # never fire on train gold: hard
                if broken:
                    why, hard = "\n".join([why or ""] + [m for _, m in broken]).strip(), True
                if why:  # a hard contradiction abstains if the re-check keeps it; a soft one only asks again
                    out = {**out, "warning": "\n".join(w for w in (out.get("warning"), why) if w),
                           "sticky": bool(out.get("warning")) or hard}
            sample["attempts"].append({"reply": text_reply, "code": code, **out})
            if out["error"] is None and not (out.get("warning") and attempt < cfg.repair):
                break
            msg = engine.repair_message(out) if out["error"] else engine.check_message(out["warning"])
            history += [{"role": "assistant", "content": text_reply}, {"role": "user", "content": msg}]
        if out["result"] is not None and out.get("warning") and out.get("sticky", True):
            out = {**out, "result": None, "error": f"unconfirmed: {out['warning']}"}  # 0 is rarely right; wrong shape is wrong
        sample["result"], sample["error"] = out["result"], out["error"]
        samples.append(sample)
    final, vote = decide([s["result"] for s in samples], cfg.min_share, cfg.unans_share)
    return {
        "id": q["id"], "business_id": q["business_id"], "question": q["question"], "as_of_date": q["as_of_date"],
        "final": finalize(final), "vote": vote, "pre": pre, "expect": vars(expect), "examples": [e["id"] for e in examples],
        "hints": hints, "samples": samples, "elapsed": round(time.time() - t0, 2),
    }


def run(cfg: RunConfig) -> dict:
    split_dir = Path(cfg.data_root) / cfg.split
    out_dir = Path(cfg.out_dir)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    from datamate import dm
    from datamate.ingest.business import INGEST_VERSION

    meta = {**asdict(cfg), "semantic_version": dm.SEMANTIC_VERSION, "ingest_version": INGEST_VERSION,
            "store_version": STORE_VERSION}
    (out_dir / "config.json").write_text(json.dumps(meta, default=str, indent=2), encoding="utf-8")

    questions = _select(read_jsonl(split_dir / "questions.jsonl"), cfg)
    sub_path = out_dir / "submission.jsonl"
    done = set()
    if cfg.resume and sub_path.exists():
        done = {json.loads(line)["id"] for line in sub_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo = [q for q in questions if q["id"] not in done]

    llm = make_llm(cfg)
    system = ENGINES[cfg.engine].build_system((split_dir / "rulebook.md").read_text(encoding="utf-8"))
    bank = ExampleBank.from_train(Path(cfg.data_root) / "train", cfg.examples_path,
                                  normalize=None if cfg.normalize == "off" else (lambda t: rules_normalize(t).text))
    cal = load_calendar(split_dir)
    lock = threading.Lock()
    t_start = time.time()
    _LLM_DOWN.clear()

    by_biz: dict[str, list[dict]] = {}
    for q in todo:
        by_biz.setdefault(q["business_id"], []).append(q)

    with sub_path.open("a", encoding="utf-8") as sub, tqdm(total=len(todo), desc="questions") as bar:
        for biz_id in sorted(by_biz):
            biz = load_business(split_dir, biz_id, cfg.cache_dir)
            roles = {n: ti.roles for n, ti in biz.info.items()}
            sales, sales_desc = build_sales(biz.tables, roles)
            index = EntityIndex.from_tables(biz.tables, roles, sales) if cfg.normalize != "off" else None
            if cfg.engine == "sql":
                store = ensure_store(split_dir, biz_id, Path(cfg.cache_dir).parent)
                pool = SqlPool(store, name_index(biz.tables, sales), n=cfg.exec_workers, timeout=cfg.timeout)
                biz_part = business_part(biz_id, biz.readme.text, sql_card(biz, sales, sales_desc, pool.macros()))
            else:
                biz_part = business_part(biz_id, biz.readme.text, schema_card(biz) + "\n\n## " + sales_desc)
                pool = ExecPool(split_dir, biz_id, cfg.cache_dir, n=cfg.exec_workers, timeout=cfg.timeout)
            try:
                with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
                    futs = [ex.submit(solve_question, q, llm, pool, system, biz_part, bank, cfg, cal, index)
                            for q in by_biz[biz_id]]
                    for f in as_completed(futs):
                        try:
                            rec = f.result()
                        except LLMUnavailable:
                            _LLM_DOWN.set()
                            ex.shutdown(wait=True, cancel_futures=True)
                            raise
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
    from datamate.eval.compare import outcomes

    rep["outcomes"] = outcomes(Path(out_dir), rep["per_question"])
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
