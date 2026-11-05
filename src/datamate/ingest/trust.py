"""Check column roles against the values: a header can lie, the cells cannot.

`role_problem` judges one column; the model's role proposals must pass it. `assess` cross-checks the sales table and gives
each capability a state: trusted (roles present, checks pass), suspect (a check failed: warn the model, drop the shadow
check), absent (no role for it, which does not make a question unanswerable: the raw tables may still hold the field).
"""

from dataclasses import dataclass, field

import pandas as pd

from datamate.ingest.business import Business
from datamate.ingest.parsers import parse_date

MIN_SHARE = 0.95
# capability -> columns of sales_lines it needs
CAPABILITIES = {"revenue": ("amount", "date"), "orders": ("order_id", "date"), "customers": ("customer", "date"),
                "products": ("product_key", "qty_base")}


def _number_share(s: pd.Series) -> float:
    return pd.to_numeric(s, errors="coerce").notna().mean()


def _text_share(s: pd.Series) -> float:
    return s.map(lambda v: isinstance(v, str)).mean()


def role_problem(df: pd.DataFrame, col: str, role: str) -> str | None:
    """Why the column's values do not fit the role, or None."""
    s = df[col].dropna()
    if s.empty:
        return "column is empty"
    dated = pd.api.types.is_datetime64_any_dtype(s)
    if role in ("order_date", "order_ts"):  # text dates are typed once the column has the role
        return None if dated or s.map(lambda v: parse_date(v, 2000, True) is not None).mean() >= MIN_SHARE else "values are not dates"
    if dated:
        return "values are dates"
    if role in ("line_amount", "unit_price", "qty", "qty_base"):
        if _number_share(s) < MIN_SHARE:
            return "values are not numbers"
        if (pd.to_numeric(s, errors="coerce") >= 0).mean() < 0.5:  # vouchers and returns are negative, but never the bulk
            return "most values are negative"
    if role == "order_id" and len(s) < 0.5 * len(df):
        return "most rows have no value"
    if role in ("status", "unit") and (s.nunique() > 30 or _text_share(s) < MIN_SHARE):
        return "too many different values for a label"
    if role in ("product_name", "customer_name") and _text_share(s) < 0.8:
        return "values are not names"
    return None


def _amount_mismatch(sales: pd.DataFrame) -> str | None:
    if not {"qty", "unit_price", "amount"} <= set(sales.columns):
        return None
    q, p, a = (pd.to_numeric(sales[c], errors="coerce") for c in ("qty", "unit_price", "amount"))
    rows = q.notna() & p.notna() & a.notna() & (a != 0)
    if rows.sum() < 30:
        return None
    share = ((q * p - a).abs()[rows] <= 0.25 * a.abs()[rows]).mean()
    return None if share >= 0.5 else f"quantity × price matches the amount on only {share:.0%} of lines"


def _period_mismatch(biz: Business, sales: pd.DataFrame) -> str | None:
    end, last = biz.readme.data_end, sales["date"].max()
    if end is None or pd.isna(last):
        return None
    return None if abs((end - last).days) <= 62 else f"sales end on {last:%Y-%m-%d}, README says {end:%Y-%m-%d}"


def _unknown_codes(biz: Business, sales: pd.DataFrame) -> str | None:
    codes = sales["product_id"].dropna() if "product_id" in sales.columns else pd.Series(dtype=object)
    lists = [biz.tables[name][c] for name, ti in biz.info.items() for c, r in ti.roles.items()
             if r == "product_id" and "product_name" in ti.roles.values() and not {"line_amount", "order_id"} & set(ti.roles.values())]
    if codes.empty or not lists or max(codes.isin(s).mean() for s in lists) >= 0.9:
        return None
    return "few product codes on the sales lines are in the product list"


@dataclass
class Trust:
    states: dict[str, str]
    problems: list[str] = field(default_factory=list)

    def note(self) -> str:
        return ("Data checks failed, treat these numbers with care: " + "; ".join(self.problems)) if self.problems else ""


def assess(biz: Business, sales: pd.DataFrame | None) -> Trust:
    problems = [f"{name}.{c}: role {r} but {p}" for name, ti in biz.info.items() for c, r in ti.roles.items()
                if "line_amount" in ti.roles.values() and c in biz.tables[name].columns and (p := role_problem(biz.tables[name], c, r))]
    if sales is None:
        return Trust({c: "absent" for c in CAPABILITIES}, problems)
    sale_problems = [p for p in (_amount_mismatch(sales), _period_mismatch(biz, sales), _unknown_codes(biz, sales)) if p]
    bad = "suspect" if sale_problems else "trusted"
    states = {c: bad if set(need) <= set(sales.columns) else "absent" for c, need in CAPABILITIES.items()}
    return Trust(states, problems + sale_problems)
