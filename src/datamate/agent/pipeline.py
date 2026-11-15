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

from datamate.agent import gates
from datamate.agent.hints import time_hints
from datamate.agent.prompt import build_system, business_part, check_message, extract_code, question_part, repair_message
from datamate.agent.retrieve import ExampleBank
from datamate.audit import audit, load_known
from datamate.agent.calibrate import features, load as load_calibrator, p_correct, tested_names
from datamate.agent.columns import resolve_columns
from datamate.agent.frame import Frame, build_frame, consistency, rulebook_words
from datamate.capabilities import Capabilities, capability_of, card_part as caps_card, profile
from datamate.agent.judge import doubt, judge_messages, parse_verdict, retry_note, solver_text
from datamate.agent.lint import frame_advice, grain_advice, ready_terms, term_advice
from datamate.agent.sandbox import ExecPool, name_index
from datamate.agent.statuses import resolve_statuses
from datamate.slots.base import ORDER as SLOT_ORDER, resolve_slots
from datamate.agent.vote import answer_key, decide
from datamate.canon import build_sales
from datamate.facts import build_facts
from datamate.eval.scorer import read_jsonl, score
from datamate.eval.semantic import CHECKS, is_gated
from datamate.fast import fast_answer
from datamate.ingest import load_business, load_calendar
from datamate.ingest.business import slots_path
from datamate.ingest.trust import assess
from datamate.ingest.schema_card import schema_card
from datamate.postprocess import Expect, check_answer, date_window, expect_answer, finalize, fit_money, fit_month, invariants
from datamate.preprocess import EntityIndex, rules_normalize
from datamate.preprocess.rewrite import names_block, rewrite
from datamate.rules import TRAIN_RULEBOOK, params_off, rule_delta, rule_gate, rule_note, rule_params
from datamate.sql.agent import (MACROS, PRIMITIVES, SqlPool, build_sql_system, extract_sql, hint_periods, sql_card,
                                sql_check_message, sql_question_part, sql_repair_message, sql_shadow)
from datamate.agent.evidence import pick_rules
from datamate.sql.compile import compile_delta, compile_rulebook, data_end, usable
from datamate.sql.shopviews import card_part, shop_views
from datamate.sql.store import STORE_VERSION, ensure_store
from datamate.sql.values import subjective_note


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
    keep_tables: Path | None = None  # experiment: JSON of question id -> raw tables to show, null = all (SQL engine)
    oracle: Path | None = None  # experiment: JSON {"notes": {question id: note}, "setup": {business: [SQL]}} (debug/oracle)
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
    fast: bool = False  # answer questions a closed frame explains in full without the model
    think_rewrite: bool = False  # thinking also for the rewrite and the one-off ingest/rule questions (short budgets)
    # SQL engine: "full" = the hand-written formula catalog; "primitives" = only generic helpers, no rulebook macros,
    # catalog advice, contract checks or rulebook lints: the model computes every business term from the rulebook itself;
    # "compiled" = primitives plus the macros the model writes from the split's rulebook at the start of the run (sql/compile.py)
    catalog: str = "full"
    # catalog "delta": also compile macros for the new/changed rules. Off by default: on public 4/176 answers called one,
    # none right, for 21-28 min of prepare; the delta lines still drive the router and the per-question rulebook lines
    compile_delta: bool = False
    shop_views: bool = False  # SQL engine: the model writes views per shop from its README and raw tables (sql/shopviews.py)
    context: str = "base"  # base | rich (SQL): card with keys, missing facts and macro SQL; rulebook lines per question; plan lines
    judge: str = "off"  # off | unans | all: a second agent checks UNANSWERABLE verdicts (unans) or every verdict (all)
    think_max_tokens: int = 8192  # compile, shop views and judge calls; deep-tier solver calls of the router
    prepare_only: bool = False  # run the one-off agent steps (rulebook macros, shop views) and stop before the questions
    # thinking router (--thinking route): thinking off by default; thinking on (and the judge) for a question that reads a
    # rule this rulebook changed, comes from a shop whose sales are suspect, or is worded unlike any train question of
    # another shop, and for one whose first answer failed, abstained, was UNANSWERABLE, hedged or tripped a check
    think_route: bool = False
    route_far: float = 0.75  # nearest train question of another shop below this similarity = unfamiliar (~12% of train, LOBO)
    calibrator: Path | None = None  # JSON from `datamate calibrate`: answers whose p_correct is below its threshold abstain
    gates: bool = True  # per-question decision gates measured on train (agent/gates.py)
    route_residual: int = 3  # router: this many unexplained question words (frame residual) send a question deep
    # wall clock (epoch seconds; the organizers' 6 h include model loading): after `deadline` questions are not solved
    # (a missing answer is an abstain), after `deadline - deep_margin` no question goes to the deep tier
    deadline: float | None = None
    deep_margin: float = 3600.0
    frame: bool = False  # print the question's frame (agent/frame.py) in the prompt
    rewrite: str = "always"  # always | residual: the LLM rewrite only when the rule-cleaned question leaves unexplained words
    slots: str = "all"  # shop conventions the model fills before the store is built (slots/): all | none | a,b (slot names)


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


def _think(llm, cfg: "RunConfig", system: str, parts: list[str], history: list[dict]) -> str:
    """A one-off agent step (rulebook macros, shop views), greedy with the deep budget. A reply that thinking cut empty
    (finish 'length') is asked again with thinking off: the public rulebook's 9 compile sections all came back empty."""
    reply = _generate(llm, cfg, system, parts, history, temperature=0.0, seed=cfg.seed, max_tokens=cfg.think_max_tokens)
    if not reply.strip() and getattr(reply, "finish_reason", None) == "length" and hasattr(llm, "without_thinking"):
        cut = reply
        reply = _generate(llm.without_thinking(), cfg, system, parts, history, temperature=0.0, seed=cfg.seed,
                          max_tokens=cfg.think_max_tokens)
        reply.cut = cut  # the compile record keeps why the first reply was empty
    return reply


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

    return LocalLLM(cfg.base_url, cfg.model, max_tokens=cfg.max_tokens, enable_thinking=cfg.enable_thinking, effort=cfg.effort)


def aux_llm(llm, cfg: RunConfig):
    """The model for short calls: thinking off unless cfg.think_rewrite."""
    return llm if cfg.think_rewrite or not hasattr(llm, "without_thinking") else llm.without_thinking()


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


def preprocess(q: dict, llm, cfg: RunConfig, index: EntityIndex | None,
               rewrite_if: Callable | None = None) -> tuple[str, dict | None, dict]:
    """(rule-cleaned text, validated LLM rewrite or None, log record) for one question.
    rewrite_if(rules): whether the LLM rewrite is needed (cfg.rewrite == "residual": only when words stay unexplained)."""
    if cfg.normalize == "off":
        return q["question"], None, {}
    rules = rules_normalize(q["question"], index)
    rec = {"rules": rules.text, "changes": rules.changes, "amounts": rules.amounts,
           "mentions": [{"kind": m.kind, "name": m.name, "full": m.full} for m in rules.mentions]}
    rw = None
    if cfg.normalize == "llm" and rewrite_if is not None and not rewrite_if(rules):
        rec["rewrite_skipped"] = True
    elif cfg.normalize == "llm":
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


def fast_pass(questions: list[dict], cal, sales, trust, off: frozenset[str], index: EntityIndex | None, save) -> list[dict]:
    """Answer what the fast branch can and return the questions left for the model."""
    left = []
    for q in questions:
        t0 = time.time()
        got = fast_answer(rules_normalize(q["question"], index).text, q["as_of_date"], cal, sales, trust, off)
        if got is None:
            left.append(q)
            continue
        answer, frame = got
        save({"id": q["id"], "business_id": q["business_id"], "question": q["question"], "as_of_date": q["as_of_date"], "final": finalize(answer),
              "fast": {"check": frame.check.name, "period": frame.call}, "samples": [], "elapsed": round(time.time() - t0, 2)})
    return left


def route_reasons(text: str, rules: list[str], off: frozenset[str], delta: frozenset[str], suspect: bool, near: float,
                  cfg: RunConfig, frame: Frame | None = None) -> list[str]:
    """Why a question goes to the deep tier before any attempt (thinking router); [] keeps it in the normal tier.
    rules: the rulebook lines the question's words point to; delta: the lines this rulebook adds or changes;
    frame: its capability state (suspect: ingest may have missed the fact) and its unexplained words."""
    out = []
    lines = {r.split("] ", 1)[1] if r.startswith("[") and "] " in r else r for r in rules}  # pick_rules: "[section] line"
    if is_gated(text, off) or lines & delta:
        out.append("changed rule")
    if suspect:
        out.append("suspect sales")
    if near < cfg.route_far:
        out.append(f"unfamiliar ({near:.2f})")
    if frame is not None and frame.capability[1] == "suspect":
        out.append(f"suspect {frame.capability[0]}")
    if frame is not None and len(frame.residual) >= cfg.route_residual:
        out.append(f"residual {len(frame.residual)}")
    return out


def escalate_reasons(final: dict, samples: list[dict], hedge: str | None) -> list[str]:
    """Why a normal-tier answer gets a deep-tier try: the signs that a first answer is often wrong."""
    out = []
    if final["type"] in ("abstain", "unanswerable"):
        out.append(final["type"])
    if hedge:
        out.append("hedge")
    if any(a.get("warning") for s in samples for a in s["attempts"]):
        out.append("check")
    if any(a.get("finish_reason") == "length" for s in samples for a in s["attempts"]):
        out.append("length")
    return out


def solve_question(q: dict, llm, pool: ExecPool | SqlPool, system: str, biz_part: str, bank: ExampleBank, cfg: RunConfig,
                   cal=None, index: EntityIndex | None = None, off: frozenset[str] = frozenset(), terms: frozenset[str] = frozenset(),
                   macros=(), rulebook: str = "", oracle_note: str | None = None, deep_llm=None,
                   delta: frozenset[str] = frozenset(), suspect: bool = False, calibrator: dict | None = None,
                   caps: Capabilities | None = None, revenue_total: float | None = None, shop: str = "clean",
                   scope: dict | None = None) -> dict:
    t0 = time.time()
    if cfg.deadline and t0 > cfg.deadline:
        return {"id": q["id"], "business_id": q["business_id"], "question": q["question"], "as_of_date": q["as_of_date"],
                "final": {"type": "abstain"}, "skipped": "deadline", "samples": [], "elapsed": 0.0}
    late = bool(cfg.deadline and t0 > cfg.deadline - cfg.deep_margin)
    if late:
        deep_llm = None  # late in the run: the normal tier only (no deep route, escalation or judge)
    engine = ENGINES[cfg.engine]
    vocab = rulebook_words(rulebook) | rulebook_words(" ".join(getattr(pool, "units", ())))  # and the shop's units

    def rewrite_if(rules) -> bool:  # the rule-cleaned text leaves words that no slot explains
        names = [m.name for m in rules.mentions if m.full]
        return bool(build_frame(rules.text, time_hints(rules.text, q["as_of_date"], cal), names, [], None, (None, None),
                                macros, MACROS, vocab).residual)

    text, rw, pre = preprocess(q, aux_llm(llm, cfg), cfg, index, rewrite_if if cfg.rewrite == "residual" else None)
    full = cfg.catalog in ("full", "delta")
    expect = expect_answer(text, rw)
    norm = {**pre.get("norm", {}), "expect": _expect_text(expect) if expect.hard else None}
    if cfg.engine == "sql":  # values and qualities the question names, found without a model call (sql/values.py)
        named = tuple(m["name"] for m in pre.get("mentions", []) if m.get("full"))
        norm["values"] = pool.values.hints(text, named)
        norm["subjective"] = subjective_note(text, rulebook, named)
        if cfg.context == "rich":
            norm["rules"] = pick_rules(rulebook, text)
    lacks = (lambda ex: caps.states.get(capability_of(ex["question"]) or "", "") == "absent") if caps else None
    examples = bank.query(q["question"], exclude_business=q["business_id"], k=cfg.k_examples, drop=lacks)
    near = bank.nearest(q["question"], q["business_id"])
    hints = time_hints(text, q["as_of_date"], cal)
    named = [m["name"] for m in pre.get("mentions", []) if m.get("full")]
    frame = build_frame(text, hints, named, norm.get("values") or [], _expect_text(expect),
                        caps.state_of(text) if caps else (None, None), macros, MACROS, vocab)
    if cfg.frame:
        norm["frame"] = frame.block()
    expect.window = date_window(text, hints)
    shadow = None if is_gated(text, off) or not full else engine.shadow(text, hints)
    extra = {"periods": hint_periods(hints)} if cfg.engine == "sql" else {}  # a period outside the data makes 'no rows' a reason
    user_parts = [biz_part, engine.question_part(q["question"], q["as_of_date"], examples, hints, norm)]
    if oracle_note:  # experiment: what an intervention tells the model, checked by hand (debug/oracle)
        user_parts[1] += "\n\nVerified facts about this shop's data for this question (use them):\n" + oracle_note
    route = None
    if cfg.think_route and deep_llm is not None:
        reasons = route_reasons(text, norm.get("rules") or pick_rules(rulebook, text), off, delta, suspect, near, cfg, frame)
        route = {"tier": "deep" if reasons else "normal", "reasons": reasons}

    def attempt(model) -> tuple[list[dict], dict, dict, str | None]:
        samples = _samples(model, q, pool, system, user_parts, text, expect, shadow, extra, terms, macros, full, cfg, frame)
        final, vote = decide([s["result"] for s in samples], cfg.min_share, cfg.unans_share)
        hedge = next((h for s in samples for a in s["attempts"] for h in [doubt(a.get("reply"))] if h), None)
        return samples, final, vote, None if final["type"] in ("unanswerable", "abstain") else hedge

    samples, final, vote, hedge = attempt(deep_llm if route and route["tier"] == "deep" else llm)
    if route and route["tier"] == "normal":
        why = escalate_reasons(final, samples, hedge)
        if why:
            first = {"final": final, "samples": samples}
            samples, final, vote, hedge = attempt(deep_llm)
            route = {"tier": "escalated", "reasons": why, "agree": answer_key(final) == answer_key(first["final"])
                     if "abstain" not in (final["type"], first["final"]["type"]) else None, "first": first}
    judged = None
    deep = not late and (route is None or route["tier"] != "normal")  # the judge is part of the deep tier
    if deep and cfg.judge != "off" and (final["type"] == "unanswerable" or hedge
                                        or (cfg.judge == "all" and final["type"] != "abstain")):
        final, judged = _judge(q, final, samples, deep_llm or llm, pool, system, user_parts, text, expect, rulebook, cfg,
                               shadow, extra, hedge)
    rec = {
        "id": q["id"], "business_id": q["business_id"], "question": q["question"], "as_of_date": q["as_of_date"],
        "final": finalize(final), "vote": vote, "pre": pre, "expect": vars(expect), "examples": [e["id"] for e in examples],
        "near": round(near, 3), "hints": hints, "samples": samples, "judge": judged, "route": route,
        "frame": frame.to_json(), "consistency": consistency(frame, final_code(samples)),
        "magnitude": magnitude(final, expect, revenue_total), "audit": shop,
    }
    if calibrator and rec["final"]["type"] != "abstain":
        rec["p_correct"] = p = p_correct(calibrator, features(rec, tested_names()))
        if p < calibrator["threshold"]:
            rec["calibrated_from"], rec["final"] = rec["final"], {"type": "abstain"}
    if cfg.gates:
        gates.apply(rec, shop, scope)
    rec["elapsed"] = round(time.time() - t0, 2)
    return rec


def log_summary(rec: dict) -> dict:
    """The fields the organizers' Docker guideline asks of each question's log (section 3.3), first in the file: the final
    answer, elapsed_ms, and every executed query with its output, in order (the full attempts, with the model's
    thinking, follow under `samples`)."""
    runs = [a for s in rec.get("samples") or [] for a in s.get("attempts") or [] if a.get("code")]
    retry = (rec.get("judge") or {}).get("retry") or {}
    if retry.get("code"):
        runs.append(retry)
    final = {k: v for k, v in rec["final"].items() if not k.startswith("_")}
    thinking = [a.get("thinking") or "" for s in rec.get("samples") or [] for a in s.get("attempts") or []]
    return {"id": rec["id"], "answer": final, "elapsed_ms": int(round(rec.get("elapsed", 0) * 1000)),
            "thinking": thinking if any(thinking) else None, "code": [a["code"] for a in runs],
            "output": [a.get("result") if a.get("error") is None else {"error": a["error"]} for a in runs]}


def final_code(samples: list[dict]) -> str | None:
    """The query behind the decided answer: the last query of the first sample that has a result."""
    s = next((s for s in samples if s.get("result") is not None), samples[0] if samples else {})
    return next((a.get("code") for a in reversed(s.get("attempts") or []) if a.get("code")), None)


def magnitude(final: dict, expect: Expect, revenue_total: float | None) -> float | None:
    """A money answer as a share of the shop's revenue over all its data (L7, soft): far above 1 is rarely right."""
    if not revenue_total or final.get("type") != "number" or not isinstance(final.get("answer"), (int, float)):
        return None
    return round(abs(float(final["answer"])) / revenue_total, 4)


def _samples(llm, q: dict, pool, system: str, user_parts: list[str], text: str, expect: Expect, shadow, extra: dict,
             terms: frozenset[str], macros, full: bool, cfg: RunConfig, frame: Frame | None = None) -> list[dict]:
    """cfg.samples executed samples of one model (greedy first), each with its repair round."""
    engine = ENGINES[cfg.engine]
    samples = []
    for s in range(cfg.samples):
        temperature = 0.0 if s == 0 else cfg.temperature
        history: list[dict] = []
        sample = {"seed": cfg.seed + s, "temperature": temperature, "attempts": []}
        out = {"result": None, "error": "not run", "stdout": ""}
        contract = None  # the primitive's answer, when a check found the query disagrees with it
        tries = 1 + cfg.repair
        attempt = 0
        extended = False
        while attempt < tries:
            try:
                text_reply = _generate(llm, cfg, system, user_parts, history, temperature=temperature, seed=cfg.seed + s)
            except LLMUnavailable:
                raise
            except Exception as e:  # request rejected by the model server: record and stop this sample
                out = {"result": None, "error": f"LLM error: {e!r}", "stdout": ""}
                sample["attempts"].append({"llm_error": repr(e)})
                break
            code = engine.extract(text_reply)
            advice = None
            if code and attempt == 0 and cfg.repair and full:
                advice = term_advice(text, code, terms, cfg.engine, macros) or (grain_advice(text, code) if cfg.engine == "pandas" else None) \
                    or (frame_advice(text, code, frame) if cfg.engine == "sql" else None)
            if code is None:
                out = {"result": None, "error": engine.no_code, "stdout": ""}
            elif advice:  # one rewrite, never a block: the second attempt runs as written
                out = {"result": None, "error": advice, "stdout": ""}
            else:
                out = pool.run(code, q["as_of_date"], shadow, **extra)
            if out["error"] is None and out["result"] is not None:
                out["result"] = fit_money(fit_month(out["result"], expect), text)
                why, hard = check_answer(out["result"], expect)
                broken = invariants(out["result"], text, expect.window)  # never fire on train gold: hard
                if broken:
                    why, hard = "\n".join([why or ""] + [m for _, m in broken]).strip(), True
                if why:  # a hard contradiction abstains if the re-check keeps it; a soft one only asks again
                    out = {**out, "warning": "\n".join(w for w in (out.get("warning"), why) if w),
                           "sticky": bool(out.get("warning")) or hard}
            sample["attempts"].append({"reply": text_reply, "code": code, **out,
                                       "thinking": getattr(text_reply, "reasoning", None),
                                       "finish_reason": getattr(text_reply, "finish_reason", None)})
            contract = out.get("contract") or contract
            if out["error"] is None and not (out.get("warning") and attempt < cfg.repair):
                break
            attempt += 1
            if code is None and not text_reply.strip() and getattr(text_reply, "finish_reason", None) == "length" \
                    and hasattr(llm, "without_thinking"):
                if not extended:  # the cut reply is no attempt: the reply without thinking keeps its repair round, once
                    tries, extended = tries + 1, True
                llm = llm.without_thinking()  # thinking ate the budget: the same budget again would be cut again
                continue
            msg = engine.repair_message(out) if out["error"] else engine.check_message(out["warning"])
            history += [{"role": "assistant", "content": text_reply}, {"role": "user", "content": msg}]
        if out["result"] is not None and out.get("warning") and out.get("sticky", True):
            out = {**out, "result": None, "error": f"unconfirmed: {out['warning']}"}  # 0 is rarely right; wrong shape is wrong
        if contract and out["result"] is None:  # the repair still disagrees: the definition tested on train gold answers
            out = {**out, "result": contract, "error": None, "warning": None}
        sample["result"], sample["error"] = out["result"], out["error"]
        samples.append(sample)
    return samples


def _checked(out: dict, text: str, expect: Expect) -> dict:
    """The answer checks of a solver attempt, as in solve_question: a contradiction makes it a warning."""
    if out["error"] is None and out["result"] is not None:
        out["result"] = fit_money(fit_month(out["result"], expect), text)
        why, hard = check_answer(out["result"], expect)
        broken = invariants(out["result"], text, expect.window)
        if broken:
            why = "\n".join([why or ""] + [m for _, m in broken]).strip()
        if why:
            out = {**out, "warning": "\n".join(w for w in (out.get("warning"), why) if w)}
    return out


def _judge(q, final, samples, llm, pool, system, user_parts, text, expect, rulebook, cfg, shadow, extra,
           hedge: str | None = None) -> tuple[dict, dict]:
    """The second agent's verdict on the solver's: agreement keeps it, an answerable verdict on UNANSWERABLE gets one more
    solver try with the judge's pointer, a disagreement that stays abstains. hedge: the solver answered but also wrote
    UNANSWERABLE; a judge that finds the data missing too makes it UNANSWERABLE."""
    code = next((a.get("code") for s in samples for a in reversed(s["attempts"]) if a.get("code")), None)
    js, jparts = judge_messages(rulebook, user_parts[0], q["question"], text, solver_text(final, code, hedge))
    try:
        reply = _generate(llm, cfg, js, jparts, [], temperature=0.0, seed=cfg.seed, max_tokens=cfg.think_max_tokens)
    except LLMUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        return final, {"error": repr(e)}
    verdict = parse_verdict(reply)
    rec = {"reply": reply[-2000:], "verdict": verdict}
    if verdict is None or verdict["answerable"] == (final["type"] != "unanswerable"):
        return final, {**rec, "action": "keep"}
    if final["type"] != "unanswerable":  # answered, but the judge finds the data missing
        if hedge:  # so did the solver
            return {"type": "unanswerable", "reason": verdict["where"] or hedge}, {**rec, "action": "unanswerable"}
        return {"type": "abstain"}, {**rec, "action": "abstain"}
    engine = ENGINES[cfg.engine]
    try:
        again = _generate(llm, cfg, system, user_parts + [retry_note(verdict)], [], temperature=0.0, seed=cfg.seed)
    except LLMUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        return {"type": "abstain"}, {**rec, "action": "abstain", "retry_error": repr(e)}
    code = engine.extract(again)
    out = _checked(pool.run(code, q["as_of_date"], shadow, **extra), text, expect) if code else {"result": None, "error": engine.no_code}
    rec["retry"] = {"reply": again, "code": code, **{k: v for k, v in out.items() if k in ("result", "error", "warning", "tables", "reads", "ran")}}
    res = out.get("result")
    if res is not None and res.get("type") != "unanswerable" and not out.get("warning"):
        return res, {**rec, "action": "answer"}
    return {"type": "abstain"}, {**rec, "action": "abstain"}


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
    aux = aux_llm(llm, cfg)
    deep_llm = llm.with_thinking(cfg.think_max_tokens) if cfg.think_route and hasattr(llm, "with_thinking") else None
    calibrator = load_calibrator(cfg.calibrator)
    rulebook = (split_dir / "rulebook.md").read_text(encoding="utf-8")

    def ask(system, parts):
        return _generate(aux, cfg, system, parts, [], temperature=0.0, seed=cfg.seed, max_tokens=cfg.rewrite_max_tokens)

    params = rule_params(rulebook)  # numbers the macros take from this rulebook; unreadable ones turn their macros off
    off = rule_gate(rulebook, Path(cfg.cache_dir) / split_dir.name / "rules.json", ask) | params_off(params)
    # full: the hand-written macros with their lints and checks; delta: the same, plus macros the model writes for the rules
    # this rulebook adds or changes against the train rulebook (the macros of changed rules are switched off)
    delta = cfg.catalog == "delta" and cfg.engine == "sql"
    full, compiled = cfg.catalog == "full" or delta, cfg.catalog == "compiled"
    rich = cfg.context == "rich" and cfg.engine == "sql"

    def think(system, parts, history):  # the one-off agent steps: rulebook macros, shop views
        return _think(deep_llm or llm, cfg, system, parts, history)

    tested = frozenset(m for m in MACROS if m not in PRIMITIVES)  # the hand-written rule macros of the stores
    hidden = off & tested if delta else tested  # what a compiled macro may not call
    rule_macros, changes = [], []

    def stores() -> dict[str, Path]:
        shops = sorted(p.name for p in (split_dir / "data").iterdir() if p.is_dir())
        return {b: ensure_store(split_dir, b, Path(cfg.cache_dir).parent) for b in shops}

    compile_cache = Path(cfg.cache_dir).parent / "compiled" / split_dir.name / "macros.json"
    if cfg.engine == "sql" and not full:  # no hand-written rulebook macro is offered, so no note about which of them changed
        system = build_sql_system(rulebook, primitives=True, compiled=compiled, plan=rich)
        if compiled:
            rule_macros = compile_rulebook(rulebook, stores(), think, compile_cache, model=cfg.model, hidden=hidden)
    elif delta:
        changes = rule_delta(rulebook, TRAIN_RULEBOOK.read_text(encoding="utf-8"),
                             Path(cfg.cache_dir) / split_dir.name / "rule_delta.json", ask)
        if changes and cfg.compile_delta:
            rule_macros = compile_delta(rulebook, changes, {m: MACROS[m] for m in sorted(tested)}, hidden, stores(), think,
                                        compile_cache, model=cfg.model)
        system = build_sql_system(rulebook, plan=rich, drop=hidden) + rule_note(off, replaced=True)
    else:
        system = ENGINES[cfg.engine].build_system(rulebook) + rule_note(off)
    delta_lines = frozenset(line for _, _, line in changes)
    examples_path = cfg.examples_path.with_name("examples_sql.jsonl") if cfg.engine == "sql" and cfg.examples_path else cfg.examples_path
    bank = ExampleBank.from_train(Path(cfg.data_root) / "train", examples_path,
                                  normalize=None if cfg.normalize == "off" else (lambda t: rules_normalize(t).text))
    cal = load_calendar(split_dir)
    lock = threading.Lock()
    t_start = time.time()
    _LLM_DOWN.clear()

    kept = json.loads(Path(cfg.keep_tables).read_text(encoding="utf-8")) if cfg.keep_tables else None
    oracle = json.loads(Path(cfg.oracle).read_text(encoding="utf-8")) if cfg.oracle else {"notes": {}, "setup": {}}
    by_biz: dict[str, list[dict]] = {}
    capabilities: dict[str, dict] = {}
    audits: dict[str, dict] = {}
    slot_fills: dict[str, dict] = {}
    enabled = SLOT_ORDER if cfg.slots == "all" else () if cfg.slots == "none" else tuple(cfg.slots.split(","))
    known_descs = load_known()
    for q in todo:
        by_biz.setdefault(q["business_id"], []).append(q)

    with sub_path.open("a", encoding="utf-8") as sub, tqdm(total=len(todo), desc="questions") as bar:
        def save(rec: dict) -> None:
            with lock:
                final = {k: v for k, v in rec["final"].items() if not k.startswith("_")}
                rec["slots"] = slot_fills.get(rec["business_id"])
                sub.write(json.dumps({"id": rec["id"], **final}, ensure_ascii=False) + "\n")
                sub.flush()
                (out_dir / "logs" / f"{rec['id']}.json").write_text(
                    json.dumps({**log_summary(rec), **rec}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            bar.update(1)

        for biz_id in sorted(by_biz):
            biz = load_business(split_dir, biz_id, cfg.cache_dir)
            if resolve_columns(biz, cfg.cache_dir, ask):
                biz = load_business(split_dir, biz_id, cfg.cache_dir)
            if resolve_statuses(biz, cfg.cache_dir, ask):
                biz = load_business(split_dir, biz_id, cfg.cache_dir)
            if resolve_slots(biz, split_dir, cfg.cache_dir, rulebook, lambda system, parts: think(system, parts, []),
                             cfg.model, enabled):
                biz = load_business(split_dir, biz_id, cfg.cache_dir)
            saved = slots_path(cfg.cache_dir, split_dir.name, biz_id)
            slot_fills[biz_id] = json.loads(saved.read_text(encoding="utf-8")).get("applied", {}) if saved.exists() else {}
            (out_dir / "slots.json").write_text(json.dumps(slot_fills, ensure_ascii=False, indent=1), encoding="utf-8")
            roles = {n: ti.roles for n, ti in biz.info.items()}
            sales, sales_desc = build_sales(biz.tables, roles, biz.slots)
            trust = assess(biz, sales)
            sales_desc += f"\n{trust.note()}" if trust.problems else ""
            shop_off = frozenset(c.name for c in CHECKS) if trust.states["revenue"] == "suspect" else off
            facts = build_facts(biz, sales)
            terms = ready_terms(facts)
            caps = profile(biz, trust, facts)
            capabilities[biz_id] = caps.to_json()
            (out_dir / "capabilities.json").write_text(json.dumps(capabilities, ensure_ascii=False, indent=1), encoding="utf-8")
            shop_audit = audit(biz, sales, known_descs)
            scope = gates.scope_of(shop_audit.to_json(), sales.attrs.get("sources", []) if sales is not None else [])
            audits[biz_id] = {**shop_audit.to_json(), "scope": scope}
            (out_dir / "audit.json").write_text(json.dumps(audits, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
            revenue_total = float(sales["amount"].abs().sum()) if sales is not None and "amount" in sales else None
            index = EntityIndex.from_tables(biz.tables, roles, sales) if cfg.normalize != "off" else None
            left = fast_pass(by_biz[biz_id], cal, sales, trust, shop_off, index, save) if cfg.fast else by_biz[biz_id]
            macros = []
            if cfg.engine == "sql":
                store = ensure_store(split_dir, biz_id, Path(cfg.cache_dir).parent)
                pool = SqlPool(store, name_index(biz.tables, sales), n=cfg.exec_workers, timeout=cfg.timeout, params=params,
                               rules_code=full)
                stored = pool.macros()
                macros = [m for m in stored if m not in shop_off] if full else [m for m in stored if m in PRIMITIVES]
                card = sql_card(biz, sales, sales_desc, macros, views=stored, rich=rich)
                probe = pool.base.cursor()  # a session of its own, to find what runs on this store
                probe.execute(f"set variable as_of = date '{data_end(probe)}'")  # its own variables: none set yet
                ours = usable(probe, rule_macros, hidden) if rule_macros else []
                pool.block([m for m in stored if m not in macros and m not in {r.name for r in ours}] if not full
                           else [m for m in stored if m in hidden] if delta else [])
                pool.setup(oracle["setup"].get(biz_id, []))  # after macros(): the card does not list the oracle's views
                setup = [r.temp_sql() for r in ours]
                if ours:
                    card += ("\n\n## Macros written from this split's rulebook" + (" for its new or changed rules" if delta else "")
                             + " (call one when its doc is the term the question asks; "
                             "TABLE ones go after FROM" + ("; the line under each is its SQL: if it disagrees with the "
                             "RULEBOOK, compute the term yourself" if rich else "") + ")\n"
                             + "\n".join(f"- {r.card_line(body=rich)}" for r in ours))
                if cfg.shop_views:
                    views = shop_views(biz_id, card, biz.readme.text, probe, think,
                                       Path(cfg.cache_dir).parent / "shopviews" / split_dir.name, model=cfg.model)
                    for v in views:
                        probe.execute(v.temp_sql())
                    setup = [v.temp_sql() for v in views] + setup
                    card += card_part(probe, views)
                if rich:
                    card += caps_card(caps)
                probe.close()
                pool.setup(setup)
                biz_part = business_part(biz_id, biz.readme.text, card)
                part_of = (lambda q: business_part(biz_id, biz.readme.text, sql_card(biz, sales, sales_desc, macros, kept.get(q["id"]), views=stored))) if kept else None
            else:
                biz_part = business_part(biz_id, biz.readme.text, schema_card(biz) + "\n\n## " + sales_desc)
                pool = ExecPool(split_dir, biz_id, cfg.cache_dir, n=cfg.exec_workers, timeout=cfg.timeout)
                part_of = None
            if cfg.prepare_only:
                pool.close()
                bar.update(len(by_biz[biz_id]))
                continue
            try:
                with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
                    futs = [ex.submit(solve_question, q, llm, pool, system, part_of(q) if part_of else biz_part, bank, cfg, cal, index, shop_off, terms, macros, rulebook,
                                      oracle["notes"].get(q["id"]), deep_llm=deep_llm, delta=delta_lines,
                                      suspect=trust.states["revenue"] == "suspect", calibrator=calibrator, caps=caps,
                                      revenue_total=revenue_total, shop=shop_audit.level, scope=scope)
                            for q in left]
                    for f in as_completed(futs):
                        try:
                            rec = f.result()
                        except LLMUnavailable:
                            _LLM_DOWN.set()
                            ex.shutdown(wait=True, cancel_futures=True)
                            raise
                        save(rec)
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
