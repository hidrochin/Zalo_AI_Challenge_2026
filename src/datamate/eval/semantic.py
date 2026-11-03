"""Contract tests of the semantic layer against train gold answers (no LLM).

Each check picks the train questions that ask exactly one primitive's quantity over exactly one resolved
period (strict patterns, so a match is a fair test), computes it with the primitive and compares with gold.
A definition change that breaks gold shows up here before any model run. Report: matched / exact / skipped
per check, with the misses to review.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from datamate import dm
from datamate.agent.hints import time_hints
from datamate.canon import build_sales
from datamate.eval.scorer import read_jsonl
from datamate.ingest import load_business, load_calendar
from datamate.preprocess import rules_normalize
from datamate.text import fold

_QUALIFIED = re.compile(r"\b(nhom|khach|ca (sang|chieu|toi)|kenh|mat hang|san pham|thuoc|zalo|tang|giam|so voi|ti le|ty le|"
                        r"trung binh|binh quan|moi|nao|cao nhat|top|tien mat|chuyen khoan|cod|size|ly|hon|tet|lai|"
                        r"loi nhuan|tro len|tu \d|duoi|tren|hang|mon|loai|nhan vien|tho|giao|tra|vi|the|tai quay|so don|so ngay)\b"
                        r"|%|phan tram|[≥≤<>]")


@dataclass
class Check:
    name: str
    gold_type: str
    match: Callable[[str], bool]  # folded rule-normalized question
    compute: Callable[[dm.Period], float]


def _orders_in(p):
    o = dm.orders()
    return o[dm.in_period(o["date"], p).values]


CHECKS = [
    Check("avg_order_value", "number",
          lambda f: bool(re.search(r"(trung binh|binh quan) (mot|moi|tren mot|tren moi|1) (don|hoa don)", f))
          and not re.search(r"\bkhach (nhom|hang nhom)|\bnhom\b|\bkenh\b|\bca (sang|chieu|toi)\b|khach (phai )?tra|da tru", f),
          dm.avg_order_value),
    Check("revenue", "number",
          lambda f: "doanh thu" in f and not _QUALIFIED.search(f.replace("doanh thu", ""))
          and not re.search(r"doanh thu (cua|ban) (?!(cua hang|quan|tiem|shop|nha thuoc|ca cua hang)\b)", f),
          dm.revenue),
    Check("order_count", "number",
          lambda f: bool(re.search(r"\b(bao nhieu|may|so) don( hang)?\b", f))
          and not re.search(r"huy|tra|khach|ca |giao|tro len|tu \d|tren|duoi|kenh|zalo|cod|moi|trung binh|nhom|mat hang|[≥≤<>]", f),
          lambda p: int(_orders_in(p)["counted"].sum())),
    Check("cancel_count", "number",
          lambda f: bool(re.search(r"\b(bao nhieu|may|so) don\b.*\bhuy\b|\bhuy\b.*\b(bao nhieu|may) don\b", f))
          and not re.search(r"ti le|ty le|%|phan tram|ly do|vi sao|khach|ca |kenh", f),
          lambda p: int((_orders_in(p)["status"] == "huỷ").sum())),
    Check("cancel_rate", "percent",
          lambda f: bool(re.search(r"\b(ti le|ty le)\b.*\bhuy\b|\bhuy\b.*(%|phan tram)", f))
          and not re.search(r"khach|ca |kenh|zalo|mat hang|nhom|so voi|tang|giam", f),
          lambda p: dm.round_half_up((_orders_in(p)["status"] == "huỷ").sum() / len(_orders_in(p)) * 100, 2)),
]


def find_check(text: str, answer_type: str) -> Check | None:
    """The contract check that a question (rule-normalized) with this answer type asks for, if any."""
    f = fold(text)
    return next((c for c in CHECKS if c.gold_type == answer_type and c.match(f)), None)


def shadow_value(check: Check, period_call: str) -> float:
    """Compute a check's quantity in the current dm context (set_context with the shop's sales)."""
    return check.compute(eval(period_call, {n: getattr(dm, n) for n in dm.__all__}))


def run_checks(data_root: Path, cache_dir: Path, names: list[str] | None = None, max_misses: int = 8) -> dict:
    train = Path(data_root) / "train"
    cal = load_calendar(train)
    gold = {a["id"]: a for a in read_jsonl(train / "answers.jsonl")}
    checks = [c for c in CHECKS if not names or c.name in names]
    todo = defaultdict(list)  # business -> [(check, question, text)]
    for q in read_jsonl(train / "questions.jsonl"):
        text = rules_normalize(q["question"]).text
        f = fold(text)
        for c in checks:
            if gold[q["id"]]["type"] == c.gold_type and c.match(f):
                todo[q["business_id"]].append((c, q, text))
                break
    rep = {c.name: {"matched": 0, "exact": 0, "skipped": defaultdict(int), "misses": []} for c in checks}
    for biz_id in sorted(todo):
        biz = load_business(train, biz_id, Path(cache_dir))
        sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
        for c, q, text in todo[biz_id]:
            r = rep[c.name]
            r["matched"] += 1
            if sales is None:
                r["skipped"]["no sales_lines"] += 1
                continue
            calls = [h.split(" -> ")[1].split(" = ")[0].split("  #")[0] for h in time_hints(text, q["as_of_date"], cal)]
            if len(calls) != 1:
                r["skipped"]["periods != 1"] += 1
                continue
            dm.set_context(q["as_of_date"], cal, sales)
            try:
                value = c.compute(eval(calls[0], {n: getattr(dm, n) for n in dm.__all__}))
            except Exception as e:  # noqa: BLE001 - a failing primitive is a finding, not a crash
                r["skipped"][f"error {type(e).__name__}"] += 1
                continue
            g = gold[q["id"]]["answer"]
            if abs(value - g) < 1e-6:
                r["exact"] += 1
            elif len(r["misses"]) < max_misses:
                r["misses"].append(f"{biz_id} {q['id']} {q['question'][:70]!r} gold={g} got={value}")
    for r in rep.values():
        r["skipped"] = dict(r["skipped"])
    return {"semantic_version": dm.SEMANTIC_VERSION, "checks": rep}
