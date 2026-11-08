"""Experiments F1-F4 of the fast branch (no LLM): precision and coverage, and the gates that must send a question away."""

from collections import Counter
from pathlib import Path

from datamate.canon import build_sales
from datamate.eval.scorer import is_correct, read_jsonl
from datamate.fast import fast_answer, parse_frame
from datamate.ingest import load_business, load_calendar
from datamate.ingest.trust import Trust, assess
from datamate.preprocess import rules_normalize

# Added to a question the parser accepted; each one changes what is asked.
CLAUSES = ["vì giao trễ", "của khách Lan", "ca sáng", "kênh Zalo", "mặt hàng Bia", "không tính đơn ship", "trừ ngày lễ", "riêng chi nhánh 2",
           "so với tháng trước", "từ khách quen"]


def _shop(split: Path, biz_id: str, cache_dir: Path):
    biz = load_business(split, biz_id, cache_dir)
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
    return sales, assess(biz, sales)


def run_fast_checks(data_root: Path, cache_dir: Path) -> dict:
    train = Path(data_root) / "train"
    cal = load_calendar(train)
    gold = {a["id"]: a for a in read_jsonl(train / "answers.jsonl")}
    by_biz: dict[str, list] = {}
    for q in read_jsonl(train / "questions.jsonl"):
        by_biz.setdefault(q["business_id"], []).append(q)
    f1, per_check, misses, escaped, tried = Counter(), Counter(), [], 0, 0
    gated = {"off": 0, "suspect": 0, "answered": 0}
    for biz_id, qs in sorted(by_biz.items()):
        sales, trust = _shop(train, biz_id, Path(cache_dir))
        for q in qs:
            text = rules_normalize(q["question"]).text
            got = fast_answer(text, q["as_of_date"], cal, sales, trust)
            f1["questions"] += 1
            if got is None:
                continue
            answer, frame = got
            ok = is_correct(answer, gold[q["id"]])
            f1["answered"] += 1
            f1["correct"] += ok
            per_check[f"{frame.check.name} {'ok' if ok else 'wrong'}"] += 1
            if not ok:
                misses.append(f"{biz_id} {q['id']} {q['question'][:70]!r} gold={gold[q['id']]['answer']} got={answer.get('answer')}")
            for clause in CLAUSES:  # F3
                tried += 1
                escaped += parse_frame(f"{text} {clause}", q["as_of_date"], cal) is None
            gated["answered"] += 1  # F4
            gated["off"] += fast_answer(text, q["as_of_date"], cal, sales, trust, frozenset({frame.check.name})) is None
            gated["suspect"] += fast_answer(text, q["as_of_date"], cal, sales, Trust({k: "suspect" for k in trust.states})) is None
    public = Path(data_root) / "public_test"
    f2 = Counter()
    if (public / "questions.jsonl").exists():  # F2: count only, public has no answers
        pub_cal = load_calendar(public)
        for q in read_jsonl(public / "questions.jsonl"):
            f2["questions"] += 1
            f2["parsed"] += parse_frame(rules_normalize(q["question"]).text, q["as_of_date"], pub_cal) is not None
    return {"F1": {**f1, "precision": round(f1["correct"] / max(f1["answered"], 1), 4), "coverage": round(f1["answered"] / max(f1["questions"], 1), 4),
                   "by_check": dict(per_check), "misses": misses[:12]},
            "F2": {**f2, "coverage": round(f2["parsed"] / max(f2["questions"], 1), 4)},
            "F3": {"tried": tried, "sent_to_model": escaped, "rate": round(escaped / max(tried, 1), 4)},
            "F4": gated}
