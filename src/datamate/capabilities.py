"""What a shop's data can answer (L2): one state per capability, read by the card, the example bank and the decision.

trusted: the fact or the sales columns exist and their values pass the checks; suspect: the fact exists but a value check
failed, or ingest found no fact while a raw table's headers name the thing (the rules may have missed it: never treat it
as missing); absent: nothing names it. Only `absent` may make a question unanswerable without the model reading the raw
tables. The value checks (L0) hold on every train shop.
"""

import re
from dataclasses import asdict, dataclass, field

import pandas as pd

from datamate.facts import Facts
from datamate.ingest.business import Business
from datamate.ingest.trust import Trust
from datamate.text import fold

MIN_SHARE = 0.95

# capability -> (fact attributes that hold it, words of a raw header or table name that name it, words of a question)
KINDS = {
    "debt": (("payments",), r"thanh toan|da tra|con no|cong no|tra truoc|tien no|\bno\b|ghi no|mua chiu",
             r"\bno\b|cong no|tra no|thu no|ghi no|mua chiu|tuoi no"),
    "deliveries": (("deliveries",), r"\bgiao\b|van chuyen|\bdot\b|chuyen xe|chuyen hang",
                   r"\bgiao\b|dot giao|chuyen hang|chuyen xe"),  # not 'chuyển khoản'

    "imports": (("imports",), r"\bnhap\b|nha cung cap|\bncc\b", r"\bnhap\b|nha cung cap|\bncc\b"),
    "expenses": (("expenses",), r"chi phi|khoan chi|chi tieu|\bchi\b", r"chi phi|lai rong|loi nhuan rong"),
    "stock": (("stock_card", "stock_report"), r"\bton\b|kiem kho|the kho", r"\bton\b|het hang|trong kho|kiem kho"),
    "lots": (("lots",), r"\blo\b|han dung|han su dung|\bhsd\b|\bexpiry\b", r"\blo\b|han dung|het han|\bhsd\b|han su dung"),
    "cost": (("cost_col", "catalog_cost"), r"gia von|\bvon\b|gia nhap", r"lai gop|gia von|loi nhuan|tien lai|\blai (bao|may|duoc|nhieu)"),
}


@dataclass
class Capabilities:
    states: dict[str, str]
    sources: dict[str, str] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)

    def state_of(self, question: str) -> tuple[str | None, str | None]:
        """(capability, state) of the first non-sales capability the question's words name, else (None, None)."""
        cap = capability_of(question)
        return cap, self.states.get(cap) if cap else None

    def to_json(self) -> dict:
        return asdict(self)


def capability_of(question: str) -> str | None:
    f = fold(question)
    return next((c for c, (_, _, q) in KINDS.items() if re.search(q, f)), None)


def _share(ok: pd.Series) -> float:
    return float(ok.mean()) if len(ok) else 1.0


def value_problems(f: Facts) -> dict[str, str]:
    """Value checks of the facts: a fact that fails one is suspect, never dropped."""
    out = {}
    if f.payments is not None and f.orders is not None and "total" in f.orders and len(f.payments):
        paid = f.payments.groupby("order_id")["amount"].sum()
        total = f.orders.drop_duplicates("order_id").set_index("order_id")["total"].reindex(paid.index)
        ok = (paid <= total.abs() * 1.01 + 1) | total.isna()
        if _share(ok) < MIN_SHARE:
            out["debt"] = f"payments above the order total on {1 - _share(ok):.0%} of paid orders"
    if f.deliveries is not None and f.orders is not None and len(f.deliveries):
        day = f.orders.drop_duplicates("order_id").set_index("order_id")["date"]
        d = f.deliveries.assign(order_day=f.deliveries["order_id"].map(day)).dropna(subset=["order_day", "date"])
        ok = pd.to_datetime(d["date"]) >= pd.to_datetime(d["order_day"])
        if _share(ok) < MIN_SHARE:
            out["deliveries"] = f"delivery before the order date on {1 - _share(ok):.0%} of trips"
    if f.imports is not None and {"qty", "price", "amount"} <= set(f.imports.columns):
        d = f.imports.dropna(subset=["qty", "price", "amount"])
        d = d[(d["amount"] != 0)]
        ok = ((d["qty"] * d["price"] - d["amount"]).abs() <= d["amount"].abs() * 0.02 + 1)
        if len(d) >= 20 and _share(ok) < 0.9:
            out["imports"] = f"amount differs from qty × price on {1 - _share(ok):.0%} of import lines"
    if f.stock_card is not None and "balance" in f.stock_card and len(f.stock_card):
        ok = f.stock_card["balance"].dropna() >= 0
        if _share(ok) < MIN_SHARE:
            out["stock"] = f"negative stock on {1 - _share(ok):.0%} of stock-card rows"
    if f.lots is not None and "expiry" in f.lots and len(f.lots):
        ok = f.lots["expiry"].notna()
        if _share(ok) < MIN_SHARE:
            out["lots"] = f"no expiry date on {1 - _share(ok):.0%} of lots"
    if f.expenses is not None and "amount" in f.expenses and len(f.expenses):
        ok = f.expenses["amount"].dropna() > 0
        if _share(ok) < MIN_SHARE:
            out["expenses"] = f"non-positive amount on {1 - _share(ok):.0%} of expenses"
    return out


def _named(biz: Business, words: str) -> str | None:
    """A raw table whose name or a header names the capability, as 'table.column'."""
    rx = re.compile(words)
    for name, t in biz.tables.items():
        if rx.search(fold(name.replace("_", " "))):
            return name
        col = next((c for c in t.columns if rx.search(fold(str(c).replace("_", " ")))), None)
        if col is not None:
            return f"{name}.{col}"
    return None


def profile(biz: Business, trust: Trust, facts: Facts) -> Capabilities:
    states = dict(trust.states)
    sources = {c: "sales_lines" for c, s in states.items() if s != "absent"}
    problems = {c: "sales checks failed" for c, s in states.items() if s == "suspect"}
    bad = value_problems(facts)
    for cap, (attrs, header, _) in KINDS.items():
        have = next((a for a in attrs if getattr(facts, a) is not None), None)
        if have:
            states[cap], sources[cap] = ("suspect" if cap in bad else "trusted"), f"fact {have}"
            if cap in bad:
                problems[cap] = bad[cap]
            continue
        named = _named(biz, header)
        states[cap] = "suspect" if named else "absent"
        if named:
            sources[cap], problems[cap] = f"raw {named}", "no fact built; a raw table names it"
    return Capabilities(states, sources, problems)


NOT_IN = {"debt": "payments received for orders, so debts (công nợ, trả nợ, khách nợ)",
          "deliveries": "delivery trips (giao hàng, đợt giao)", "imports": "import receipts (nhập hàng, nhà cung cấp, giá nhập)",
          "expenses": "expenses (chi phí, lãi ròng)", "stock": "stock (tồn kho)", "lots": "lots and expiry dates (lô, hạn dùng)",
          "cost": "a cost price (giá vốn)"}


def card_part(caps: Capabilities) -> str:
    """The card's block: what the shop lacks (absent) and what ingest could not read cleanly (suspect)."""
    absent = [NOT_IN[c] for c in NOT_IN if caps.states.get(c) == "absent"]
    suspect = [f"{NOT_IN[c]}: {caps.problems.get(c, '')} ({caps.sources.get(c, '')})" for c in NOT_IN
               if caps.states.get(c) == "suspect"]
    out = ""
    if absent:
        out += ("\n\n## Not in this shop's data\n" + "\n".join(f"- {m}" for m in absent)
                + "\nNo file of this shop names these: a question that needs one is UNANSWERABLE.")
    if suspect:
        out += ("\n\n## Read with care\n" + "\n".join(f"- {m}" for m in suspect)
                + "\nThe data may hold these in a raw table: look there before answering UNANSWERABLE.")
    return out
