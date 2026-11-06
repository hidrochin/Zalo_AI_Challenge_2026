"""Replay the fact helpers (facts.py through dm) on the train questions they serve and compare with gold. No model.

Each group parses its own small question shape; a question it cannot parse counts as skipped, not as a miss."""

import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from datamate import dm
from datamate.agent.hints import time_hints
from datamate.canon import build_sales
from datamate.eval.scorer import is_correct, read_jsonl
from datamate.facts import build_facts
from datamate.ingest import load_business, load_calendar
from datamate.preprocess import rules_normalize
from datamate.text import fold

GROUPS = [
    ("debt_aged", r"\btuoi no\b|\bno\b.*qua \d+ ngay|dat (da |cach do )?(qua|hon) \d+ ngay"),
    ("debt_flow", r"(no them|ghi no|mua chiu them|chiu them).*(tra|bot)"),
    ("debt_repaid", r"tra no|tra bot no|thu hoi.*cong no|thu no"),
    ("debt_count", r"(bao nhieu|may|bn|so|dem( dup| giup)?( cac)?) khach.*\b(no|cong no|thieu)\b"),
    ("debt_top", r"\b(khach|ai)\b.*\b(no|cong no)\b.*(nhieu|lon)|(nhieu|lon).*cong no|top.*\b(no|cong no)\b|\bbang\b.*\bno\b"),
    ("debt_total", r"\bcon no\b|\bcon thieu\b"),
    ("delivery_orders", r"don.*tu \d+ dot"),
    ("delivery_trips", r"dot (giao|hang)|giao.*\bdot\b|\bcho\b.*\bdot\b"),
    ("avg_import_price", r"binh quan.*nhap|nhap.*binh quan"),
    ("best_margin", r"(hang|mat hang|sp|san pham|mon) nao.*(lai gop|loi gop|loi nhat|lai nhat)"),
    ("gross_profit", r"(lai|loi) gop"),
    ("net_profit", r"(lai|loi nhuan|loi) rong"),
    ("slow_products", r"(mat hang|san pham|mon|hang) e\b|\bban (khong|ko|k|hong|chua) (duoc|dc)|khong ai mua|ko ai mua"),
    ("price_effect", r"doi gia|tang gia|len gia|\bso so\b"),
    ("nearest_lot", r"\blo\b.*(gan het han|het han som)"),
    ("customer_group", r"\bkhach\b.*\bnhom\b|\bnhom (si|than thiet|le|vip)\b"),
    ("stock_split", r"(chan|nguyen).*\ble\b|\ble\b.*(chan|nguyen)"),
    ("stock", r"\bton (kho|cuoi)\b|\bcon ton\b|trong kho|kho con\b|\bcon (bao nhieu|may|lai)\b"),
]
GROUPS = [(n, re.compile(p)) for n, p in GROUPS]


def group_of(text: str) -> str | None:
    f = fold(text)
    return next((n for n, p in GROUPS if p.search(f)), None)


def _dates(f: str) -> list[pd.Timestamp]:
    return [pd.Timestamp(int(y), int(m), int(d)) for d, m, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", f)]


def _period(text: str, as_of: str, cal):
    hints = time_hints(text, as_of, cal)
    m = re.search(r"= (\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", hints[0]) if hints else None
    return dm.between(m.group(1), m.group(2)) if m else None


def _longest(f: str, names) -> str | None:
    hits = [n for n in names if isinstance(n, str) and fold(n) in f]
    return max(hits, key=lambda n: len(fold(n))) if hits else None


def solve(group: str, text: str, as_of: str, cal, sales: pd.DataFrame, facts) -> dict | None:
    """The answer of a question of this group, or None when the question has a shape this replay does not parse."""
    f = fold(text)
    days = _dates(f)
    x = days[-1] if days else None
    p = _period(text, as_of, cal)
    dm.set_context(as_of, cal, sales, facts)  # time_hints sets a context of its own
    products = sales["product_name"].dropna().unique() if "product_name" in sales.columns else []
    if dm._FACTS.orders is None and group.startswith("debt"):
        return {"type": "unanswerable", "reason": "no orders with totals"}
    if group == "debt_total":
        return dm.answer_number(dm.debt_total(x), "money")
    if group == "debt_count":
        return dm.answer_number(len(dm.debt_customers(x)), "count")
    if group == "debt_top":
        k = re.search(r"(\d+) khach|top (\d+)", f)
        if k is None:
            return dm.answer_number(dm.debt_total(x), "money")
        rows = dm.debt_customers(x).head(int(k.group(1) or k.group(2)))
        return dm.answer_table(["khach_hang", "so_tien_no"], rows[["customer_name", "debt"]].values.tolist(), "money")
    if group == "debt_aged":
        o = dm.debt_orders(x)
        n = re.search(r"(?:qua|hon) (\d+) ngay", f)
        if n:
            return dm.answer_number(o.loc[o["age"] > int(n.group(1)), "debt"].sum(), "money")
        return dm.answer_list([o.loc[o["age"] <= 7, "debt"].sum(), o.loc[(o["age"] > 7) & (o["age"] <= 15), "debt"].sum(), o.loc[o["age"] > 15, "debt"].sum()], "money")
    if group == "debt_repaid" and p:
        return dm.answer_number(dm.debt_repaid(p), "money")
    if group == "debt_flow" and p:
        who = _longest(f, dm._FACTS.orders["customer_name"].unique())
        return dm.answer_list([dm.debt_new(p, who), dm.debt_repaid(p, who)], "money") if who else None
    if group == "delivery_trips" and p:
        return dm.answer_number(dm.delivery_trips(p), "count")
    if group == "avg_import_price" and p:
        who = _longest(f, dm.imports()["product_name"].unique())
        return dm.answer_number(dm.avg_import_price(who, p), "money") if who else None
    if group == "gross_profit" and p:
        return dm.answer_number(dm.gross_profit(p), "money")
    if group == "net_profit" and p:
        return dm.answer_number(dm.net_profit(p), "money")
    if group == "slow_products":
        a = dm.between(days[0], days[1]) if len(days) >= 2 else p
        return dm.answer_number(len(dm.slow_products(a)), "count") if a else None
    if group == "price_effect":
        n, who = re.search(r"(\d+) ngay", f), _longest(f, products)
        return dm.answer_percent(dm.price_effect(who, days[-1], int(n.group(1)))) if n and who and days else None
    if group == "customer_group" and p:
        if "customer_group" not in sales.columns:
            return {"type": "unanswerable", "reason": "no customer groups"}
        name = _longest(f, sales["customer_group"].dropna().unique())
        x2 = sales[sales["counted"] & dm.in_period(sales["date"], p).values]
        part = x2.loc[x2["customer_group"] == name, "amount"].sum() if name else None
        if part is None:
            return None
        if re.search(r"trung binh|binh quan", f):
            return None
        share = re.search(r"%|phan tram|chiem|ti trong|dong gop|phan cua", f)
        return dm.answer_percent(part / x2["amount"].sum() * 100) if share else dm.answer_number(part, "money")
    if group == "best_margin" and p:
        x2 = sales[sales["counted"] & dm.in_period(sales["date"], p).values]
        margin = {n: dm.gross_profit(p, g) for n, g in x2.groupby("product_name")}
        return dm.answer_string(max(margin, key=margin.get))
    if group == "stock":
        who = _longest(f, products)
        return dm.answer_number(dm.stock(who, x), "count") if who else None
    if group == "stock_split":
        who = _longest(f, products)
        unit = dm._FACTS.units if dm._FACTS else None
        if who is None or unit is None:
            return None
        units = unit[unit["product_name"] == who].sort_values("factor", ascending=False)
        big = next((u for u in units["unit"] if u in f.replace(fold(who), " ")), None)
        return dm.answer_list(dm.stock_split(who, big, x), "count") if big else None
    if group == "nearest_lot":
        who = _longest(f, products)
        if who is None:
            return None
        end = x if x is not None else dm.AS_OF
        lots = dm.lots_left(x)
        lots = lots[(lots["product_name"] == who) & (lots["expiry"] >= end)]
        return dm.answer_date(lots["expiry"].min()) if len(lots) else None
    return None


def run_fact_checks(data_root: Path, cache_dir: Path, split: str = "train", max_misses: int = 6) -> dict:
    root = Path(data_root) / split
    cal = load_calendar(root)
    gold = {a["id"]: a for a in read_jsonl(root / "answers.jsonl")}
    by_biz = defaultdict(list)
    for q in read_jsonl(root / "questions.jsonl"):
        by_biz[q["business_id"]].append(q)
    rep: dict[str, Counter] = defaultdict(Counter)
    misses: dict[str, list] = defaultdict(list)
    for biz_id, qs in sorted(by_biz.items()):
        biz = load_business(root, biz_id, Path(cache_dir))
        sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
        if sales is None:
            continue
        facts = build_facts(biz, sales)
        for q in qs:
            text = rules_normalize(q["question"]).text
            group = group_of(text)
            if group is None:
                continue
            ref = gold[q["id"]]
            rep[group]["questions"] += 1
            try:
                got = solve(group, text, q["as_of_date"], cal, sales, facts)
            except (dm.Unanswerable, dm.NoFact):
                got = {"type": "unanswerable", "reason": "no fact"}
            except Exception as e:  # noqa: BLE001 - a helper that crashes on a real shop is a finding
                rep[group]["crashed"] += 1
                if len(misses[group]) < max_misses:
                    misses[group].append(f"{biz_id} crash {type(e).__name__}: {e}"[:150])
                continue
            if got is None:
                rep[group]["skipped"] += 1
            elif ref["type"] == "unanswerable":
                rep[group]["unanswerable right" if got["type"] == "unanswerable" else "unanswerable missed"] += 1
            elif is_correct({"id": q["id"], **got}, ref):
                rep[group]["exact"] += 1
            else:
                rep[group]["wrong"] += 1
                if len(misses[group]) < max_misses:
                    misses[group].append(f"{biz_id} {q['question'][:60]!r} gold={str(ref['answer'])[:44]} got={str(got.get('answer'))[:44]}")
    return {g: {**rep[g], "misses": misses[g]} for g in sorted(rep)}
