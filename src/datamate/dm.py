"""Helper library available inside generated programs.

Every public function here is star-imported into the program namespace and also reachable as
`dm.<name>`. `AS_OF` (today) and `CAL` (solar-lunar calendar) are set per question by the sandbox.
Periods are inclusive date ranges; `in_period` clips their end to AS_OF because records after
today "have not happened yet" (rulebook).
"""

import difflib
import inspect
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

import numpy as np
import pandas as pd

from datamate.canon import build_orders as _build_orders
from datamate.canon import is_walkin as _is_walkin
from datamate import facts as _facts
from datamate.ingest.parsers import parse_date
from datamate.text import fold, norm_key

# Version of the business definitions behind the primitives (sales_lines, orders, revenue, avg_order_value, ...).
# Bump it whenever a definition changes; it is logged with every run. `datamate semantic-check` tests the
# definitions against train gold answers.
SEMANTIC_VERSION = "2026-11-07.2"

AS_OF: pd.Timestamp | None = None
CAL: pd.DataFrame | None = None
_RESULT: dict | None = None
_SALES: pd.DataFrame | None = None
_ORDERS: dict[int, pd.DataFrame] = {}
_FACTS: _facts.Facts | None = None


class Unanswerable(Exception):
    """Raise (or call `unanswerable`) when the data cannot support the question."""


class Period(NamedTuple):
    """Inclusive date range: fields .start and .end (pd.Timestamp, midnight) and .days. Build it with the period helpers below (month, this_month, ...), not with Period(...)."""
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def days(self) -> int:
        return int((self.end - self.start).days) + 1

    def __repr__(self) -> str:
        return f"Period({self.start:%Y-%m-%d} .. {self.end:%Y-%m-%d})"


def set_context(as_of, cal: pd.DataFrame | None, sales: pd.DataFrame | None = None, facts: _facts.Facts | None = None) -> None:
    global AS_OF, CAL, _RESULT, _SALES, _FACTS
    AS_OF = pd.Timestamp(as_of).normalize()
    CAL = cal
    _RESULT = None
    _SALES = sales
    _FACTS = facts


def _d(x) -> pd.Timestamp:
    if isinstance(x, str):
        t = parse_date(x)
        if t is None:
            t = pd.Timestamp(x)
        return t.normalize()
    return pd.Timestamp(x).normalize()


# ---------------------------------------------------------------- periods

def day(d) -> Period:
    """A single day (accepts 'YYYY-MM-DD', 'dd/mm/yyyy' or Timestamp)."""
    t = _d(d)
    return Period(t, t)


def today() -> Period:
    """Hôm nay = AS_OF."""
    return day(AS_OF)


def yesterday() -> Period:
    """Hôm qua."""
    return day(AS_OF - pd.Timedelta(days=1))


def between(a, b) -> Period:
    """Từ a đến b, both inclusive."""
    return Period(_d(a), _d(b))


def until(d=None) -> Period:
    """From the start of the data to the end of day d (default today): for point-in-time questions (còn nợ, còn tồn)."""
    return Period(pd.Timestamp("1900-01-01"), _d(d if d is not None else AS_OF))


def month(y: int, m: int) -> Period:
    """Tháng m/y: whole calendar month."""
    s = pd.Timestamp(year=int(y), month=int(m), day=1)
    return Period(s, s + pd.offsets.MonthEnd(0))


def month_auto(m: int) -> Period:
    """Tháng m without a year: this year unless that month is after today, else last year."""
    y = AS_OF.year if pd.Timestamp(year=AS_OF.year, month=int(m), day=1) <= AS_OF else AS_OF.year - 1
    return month(y, m)


def this_month() -> Period:
    """Tháng này: 1st of the current month .. today."""
    return Period(AS_OF.replace(day=1), AS_OF)


def prev_month_same_days() -> Period:
    """Comparison window for 'tháng này vs tháng trước': day 1 .. same day-number of last month (capped at month end)."""
    s = (AS_OF.replace(day=1) - pd.Timedelta(days=1)).replace(day=1)
    last = (s + pd.offsets.MonthEnd(0)).day
    return Period(s, s.replace(day=min(AS_OF.day, last)))


def last_month() -> Period:
    """The month before TODAY's month (whole month). For 'tháng M so với tháng trước' use prev_period(month(Y, M))."""
    s = (AS_OF.replace(day=1) - pd.Timedelta(days=1)).replace(day=1)
    return month(s.year, s.month)


def prev_period(p: Period) -> Period:
    """The period just before p: previous month for a whole month, previous quarter/year/Mon-Sun week likewise, else the same number of days right before p."""
    s, e = p.start, p.end
    if s.day == 1 and e == s + pd.offsets.MonthEnd(0):
        t = s - pd.offsets.MonthBegin(1)
        return month(t.year, t.month)
    if s.day == 1 and s.month % 3 == 1 and e == s + pd.offsets.QuarterEnd(0):
        t = s - pd.offsets.QuarterBegin(1, startingMonth=1)
        return quarter(t.year, (t.month - 1) // 3 + 1)
    if s.month == 1 and s.day == 1 and e.month == 12 and e.day == 31 and s.year == e.year:
        return year(s.year - 1)
    return Period(s - pd.Timedelta(days=p.days), s - pd.Timedelta(days=1))


def same_period_last_year(p: Period) -> Period:
    """'Cùng kỳ năm trước': p shifted back one year (whole month stays a whole month)."""
    s, e = p.start - pd.DateOffset(years=1), p.end - pd.DateOffset(years=1)
    if p.end == p.start + pd.offsets.MonthEnd(0) and p.start.day == 1:
        e = s + pd.offsets.MonthEnd(0)
    return Period(s, e)


def days_from(d, n: int) -> Period:
    """'N ngày kể từ ngày D': D .. D+N-1."""
    t = _d(d)
    return Period(t, t + pd.Timedelta(days=int(n) - 1))


def days_before(d, n: int) -> Period:
    """'N ngày ngay trước ngày D': D-N .. D-1."""
    t = _d(d)
    return Period(t - pd.Timedelta(days=int(n)), t - pd.Timedelta(days=1))


def week_this() -> Period:
    """Tuần này: Monday of the current week .. today."""
    return Period(AS_OF - pd.Timedelta(days=AS_OF.weekday()), AS_OF)


def week_prev() -> Period:
    """Tuần trước / tuần rồi: the whole previous Monday..Sunday week."""
    s = AS_OF - pd.Timedelta(days=AS_OF.weekday() + 7)
    return Period(s, s + pd.Timedelta(days=6))


def last_n_days(n: int) -> Period:
    """N ngày gần nhất: n consecutive days ending today (today included)."""
    return Period(AS_OF - pd.Timedelta(days=int(n) - 1), AS_OF)


def quarter(y: int, q: int) -> Period:
    """Quý q/y (calendar quarters)."""
    s = pd.Timestamp(year=int(y), month=3 * (int(q) - 1) + 1, day=1)
    return Period(s, s + pd.offsets.QuarterEnd(0))


def quarter_auto(q: int) -> Period:
    """Quý q without a year: this year unless it starts after today, else last year."""
    p = quarter(AS_OF.year, q)
    return p if p.start <= AS_OF else quarter(AS_OF.year - 1, q)


def year(y: int) -> Period:
    """Năm y."""
    return Period(pd.Timestamp(year=int(y), month=1, day=1), pd.Timestamp(year=int(y), month=12, day=31))


def ytd() -> Period:
    """Từ đầu năm (tới nay): Jan 1 of this year .. today."""
    return Period(pd.Timestamp(year=AS_OF.year, month=1, day=1), AS_OF)


def _lunar(lunar_year: int, lunar_month: int, lunar_day: int) -> pd.Timestamp:
    c = CAL
    row = c[(c.lunar_year == lunar_year) & (c.lunar_month == lunar_month) & (c.lunar_day == lunar_day) & (~c.lunar_leap.astype(bool))]
    if row.empty:
        raise Unanswerable(f"lunar date {lunar_day}/{lunar_month}/{lunar_year} not in calendar")
    return pd.Timestamp(row.iloc[0]["date"]).normalize()


def tet(y: int) -> Period:
    """Tết năm y: solar date of 23 tháng Chạp (lunar y-1) .. mùng 10 tháng Giêng (lunar y)."""
    return Period(_lunar(int(y) - 1, 12, 23), _lunar(int(y), 1, 10))


def pre_tet(y: int) -> Period:
    """'Trước Tết' comparison window: the same number of days immediately before 23 tháng Chạp."""
    t = tet(y)
    return Period(t.start - pd.Timedelta(days=t.days), t.start - pd.Timedelta(days=1))


def in_period(dates, p: Period, clip: bool = True) -> pd.Series:
    """Boolean mask: dates (Series of datetimes) fall inside p; end clipped to AS_OF unless clip=False."""
    s = pd.to_datetime(pd.Series(dates)).dt.normalize()
    end = min(p.end, AS_OF) if clip else p.end
    return (s >= p.start) & (s <= end)


def require(cond, reason: str) -> None:
    """Raise Unanswerable(reason) unless cond is truthy."""
    if not cond:
        raise Unanswerable(reason)


def require_coverage(dates, p: Period, what: str = "data") -> None:
    """Unanswerable if p lies entirely outside the dates present (no data ≠ 0). Partial overlap is fine."""
    s = pd.to_datetime(pd.Series(dates)).dropna()
    if s.empty or p.end < s.min().normalize() or p.start > s.max().normalize() or p.start > AS_OF:
        raise Unanswerable(f"{p} is outside the {what} range")


def shift_of(ts):
    """'sáng' for 06:00–13:59, 'chiều' for 14:00–21:59, else None. Works on a Timestamp or a Series."""
    def one(t):
        if pd.isna(t):
            return None
        h = pd.Timestamp(t).hour
        return "sáng" if 6 <= h < 14 else ("chiều" if 14 <= h < 22 else None)
    return pd.Series(ts).map(one) if isinstance(ts, (pd.Series, list, np.ndarray)) else one(ts)


def count_weekday(p: Period, weekday: int) -> int:
    """Number of days with the given weekday (0=Monday .. 6=Sunday) in p, end clipped to AS_OF."""
    days = pd.date_range(p.start, min(p.end, AS_OF))
    return int((days.weekday == int(weekday)).sum())


# ---------------------------------------------------------------- numbers & ranking

def pct_change(new, old) -> float:
    """Percent change from old to new: (new-old)/old*100 (negative when it fell)."""
    if old == 0:
        raise Unanswerable("base period value is 0")
    return (float(new) - float(old)) / float(old) * 100.0


def round_half_up(x, nd: int = 0) -> float:
    """Round half away from zero (money: nd=0)."""
    q = Decimal(1).scaleb(-nd)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def rank_top(df: pd.DataFrame, by: str, k: int | None = None, code: str | None = None, name: str | None = None) -> pd.DataFrame:
    """Sort descending by `by`; ties by `code` ascending (else `name`); keep top k (all if fewer)."""
    keys, asc = [by], [False]
    for c in (code, name):
        if c and c in df.columns and c != by:
            keys.append(c)
            asc.append(True)
    out = df.sort_values(keys, ascending=asc, kind="mergesort")
    return out if k is None else out.head(int(k))


def find(candidates, text: str, n: int = 5, cutoff: float = 0.5) -> list[str]:
    """Fuzzy-match text (typos / missing diacritics ok) against candidate names; best first."""
    cands = [c for c in pd.unique(pd.Series(list(candidates)).dropna().astype(str))]
    key = fold(text)
    folded = {}
    for c in cands:
        folded.setdefault(fold(c), c)
    exact = [folded[k] for k in folded if k == key]
    contains = [folded[k] for k in folded if key and key in k and folded[k] not in exact]
    close = [folded[k] for k in difflib.get_close_matches(key, list(folded), n=n, cutoff=cutoff)]
    out: list[str] = []
    for c in exact + contains + close:
        if c not in out:
            out.append(c)
    return out[:n]


def sales_lines() -> pd.DataFrame:
    """Canonical sold lines of this shop (see the 'sales_lines()' note in the schema card): date, order_id, status, counted, amount, qty_base, product_name, customer, walkin + source columns."""
    if _SALES is None:
        raise RuntimeError("sales_lines() helper is not built for this shop, but the sales data is in T: "
                           "compute from the T tables in the schema card (this is not a reason for unanswerable())")
    return _SALES.copy(deep=False)


def orders() -> pd.DataFrame:
    """One row per order: order_id, date, status, counted (not cancelled/returned), goods_amount (= order revenue), n_lines, customer, walkin + header columns."""
    key = id(_SALES)
    if key not in _ORDERS:
        _ORDERS.clear()
        _ORDERS[key] = _build_orders(sales_lines())
    return _ORDERS[key].copy(deep=False)


def price_on(product: str, d, lines: pd.DataFrame | None = None) -> float:
    """'Giá bán X ngày D': unit price per BASE unit before line discount on lines of X ordered on day D (cancelled/returned orders included); the most frequent value."""
    s = sales_lines() if lines is None else lines
    names = find(s["product_name"], product, n=1)
    if not names:
        raise RuntimeError(f"product {product!r} not found in sales_lines()['product_name']")
    x = s[(s["product_name"] == names[0]) & in_period(s["date"], day(d), clip=False).values]
    if x.empty:
        raise Unanswerable(f"{names[0]} has no sale line on {_d(d):%Y-%m-%d}")
    qty = pd.to_numeric(x["qty"], errors="coerce") if "qty" in x else None
    qb = pd.to_numeric(x["qty_base"], errors="coerce")
    if "unit_price" in x and x["unit_price"].notna().any():
        per = pd.to_numeric(x["unit_price"], errors="coerce") * (qty / qb if qty is not None else 1)
    else:
        per = pd.to_numeric(x["amount"], errors="coerce") / qb
    return float(per.dropna().round(2).mode().iloc[0])


def revenue(p: Period, lines: pd.DataFrame | None = None) -> float:
    """Default-rule revenue in p: sum of `amount` over counted lines dated in p (pass filtered lines to restrict, e.g. one product)."""
    s = sales_lines() if lines is None else lines
    require_coverage(sales_lines()["date"], p, "sales")
    return float(s.loc[s["counted"] & in_period(s["date"], p).values, "amount"].sum())


def avg_order_value(p: Period, lines: pd.DataFrame | None = None) -> float:
    """'Giá trị trung bình một đơn' in p: revenue(p, lines) / number of counted orders among those lines in p, rounded half-up to đồng (train gold 167/168)."""
    s = sales_lines() if lines is None else lines
    rev = revenue(p, s)
    n = s.loc[s["counted"] & in_period(s["date"], p).values, "order_id"].nunique()
    if n == 0:
        raise Unanswerable(f"no counted order in {p}")
    return round_half_up(rev / n, 0)


def is_walkin(names) -> pd.Series:
    """True for walk-in / anonymous customers (Khách lẻ, lẻ, vãng lai, blank); exclude them from customer rankings."""
    return _is_walkin(names)


def same_name(a, b) -> bool:
    """True if two names differ only in case, spacing, punctuation or diacritics."""
    return fold(norm_key(a)) == fold(norm_key(b))


class NoFact(RuntimeError):
    """A fact helper found no table for this: the shop may not record it, or may name it in a way the helper cannot see."""


def _fact(name: str, what: str):
    v = getattr(_FACTS, name, None)
    if v is None:
        raise NoFact(f"No table for {what} was recognised in this shop. Look through T in the schema card: compute it from the "
                     "raw tables if they hold it, and call unanswerable() only if no table does.")
    return v


def _end(d=None) -> pd.Timestamp:
    """The day a state is read at: a date, or the last day of a Period, never after today."""
    return AS_OF if d is None else min(d.end if isinstance(d, Period) else _d(d), AS_OF)


def _customer(name) -> str | None:
    if name is None:
        return None
    o = _fact("orders", "orders with totals")
    names = find(o["customer_name"], name, n=1)
    if not names:
        raise RuntimeError(f"customer {name!r} not found")
    return o.loc[o["customer_name"] == names[0], "customer"].iloc[0]


def same_text(a, b) -> bool:
    """True if two texts are equal apart from case and diacritics: 'hoạt chất ghi đúng như vậy' needs this, not a substring match."""
    return fold(a) == fold(b)


def debt_orders(d=None) -> pd.DataFrame:
    """Orders still owing at the end of day d (default today): order_id, date, customer, customer_name, walkin, debt, age (days from the order date). COD, marketplace and cancelled orders are already left out."""
    _fact("orders", "orders with totals")
    _fact("payments", "payments")
    return _facts.debt_rows(_FACTS, _end(d))


def debt_total(d=None) -> float:
    """'Khách còn nợ tổng cộng' at the end of day d, walk-in orders included."""
    return float(debt_orders(d)["debt"].sum())


def debt_customers(d=None) -> pd.DataFrame:
    """Debt per customer at the end of day d, biggest first: customer, customer_name, debt. Walk-in orders have no row."""
    o = debt_orders(d)
    o = o[~o["walkin"]]
    return (o.groupby(["customer", "customer_name"], as_index=False)["debt"].sum()
            .sort_values(["debt", "customer"], ascending=[False, True], kind="mergesort").reset_index(drop=True))


def debt_repaid(p: Period, customer: str | None = None) -> float:
    """'Thu nợ, trả nợ' in p: payments dated in p, after the day of purchase, on any on-credit order (also of earlier periods)."""
    _fact("orders", "orders with totals")
    _fact("payments", "payments")
    return _facts.repaid(_FACTS, p.start, _end(p.end), _customer(customer))


def debt_new(p: Period, customer: str | None = None) -> float:
    """'Nợ thêm' in p: what the on-credit orders placed in p left unpaid at the end of the day of purchase."""
    _fact("orders", "orders with totals")
    _fact("payments", "payments")
    return _facts.new_debt(_FACTS, p.start, _end(p.end), _customer(customer))


def delivery_trips(p: Period) -> int:
    """'Số đợt giao' in p: distinct (order, trip) pairs with a delivery date in p."""
    d = _fact("deliveries", "deliveries")
    return int(d[(d["date"] >= p.start) & (d["date"] <= _end(p.end))][["order_id", "trip"]].drop_duplicates().shape[0])


def imports() -> pd.DataFrame:
    """Import lines of receipts that are not cancelled, up to today: doc, date, product_name, qty, unit, price (per import unit), amount, qty_base."""
    f = _fact("imports", "imports")
    return f[f["date"] <= AS_OF]


def avg_import_price(product: str, p: Period) -> float:
    """'Giá nhập bình quân' of a product in p, weighted by quantity: sum(qty*price)/sum(qty), in whole đồng."""
    f = imports()
    names = find(f["product_name"], product, n=1)
    x = f[(f["product_name"] == names[0]) & (f["date"] >= p.start) & (f["date"] <= p.end)] if names else f.iloc[0:0]
    if x.empty:
        raise Unanswerable(f"no import of {product} in {p}")
    return round_half_up(x["amount"].sum() / x["qty"].sum(), 0)


def gross_profit(p: Period, lines: pd.DataFrame | None = None) -> float:
    """'Lãi gộp' in p: revenue minus qty × the cost recorded on each sale line (pass filtered lines for one product)."""
    col = _fact("cost_col", "cost on its sale lines")
    s = sales_lines() if lines is None else lines
    x = s[s["counted"] & in_period(s["date"], p).values]
    return float(x["amount"].sum() - (pd.to_numeric(x[col], errors="coerce") * pd.to_numeric(x["qty"], errors="coerce")).sum())


def net_profit(p: Period) -> float:
    """'Lãi ròng' of p: gross_profit(p) minus every expense dated in p."""
    e = _fact("expenses", "expenses")
    return gross_profit(p) - float(e.loc[(e["date"] >= p.start) & (e["date"] <= _end(p.end)), "amount"].sum())


def slow_products(p: Period) -> list[str]:
    """'Hàng ế' in p: products sold before p (counted orders) with no counted order in p."""
    s = sales_lines()
    s = s[s["counted"] & s["is_goods"]]
    before = set(s.loc[s["date"] < p.start, "product_name"].dropna())
    return sorted(before - set(s.loc[in_period(s["date"], p).values, "product_name"].dropna()))


def price_effect(product: str, d, n: int) -> float:
    """'Hiệu ứng tăng giá': percent change of the quantity sold (base units), n days from day d (d included) vs the n days right before d."""
    s = sales_lines()
    names = find(s["product_name"], product, n=1)
    x = s[s["counted"] & (s["product_name"] == names[0])] if names else s.iloc[0:0]
    t, span = _d(d), pd.Timedelta(days=int(n))
    new = x.loc[(x["date"] >= t) & (x["date"] < t + span) & (x["date"] <= AS_OF), "qty_base"].sum()
    old = x.loc[(x["date"] >= t - span) & (x["date"] < t), "qty_base"].sum()
    return pct_change(new, old)


def stock(product: str | None = None, d=None):
    """Stock at the end of day d (default today) in base units: one product's number, or a Series by product_name."""
    try:
        s = _facts.stock_of(_FACTS, sales_lines(), _end(d))
    except (LookupError, AttributeError):
        raise NoFact("No stock table was recognised in this shop. Look through T in the schema card: compute it from the raw "
                     "tables if they hold it, and call unanswerable() only if no table does.") from None
    if product is None:
        return s
    names = find(s.index, product, n=1)
    if not names:
        raise Unanswerable(f"{product} has no stock record")
    return float(s[names[0]])


def stock_split(product: str, unit: str, d=None) -> list[int]:
    """'Tồn theo đơn vị lớn': [whole big units, loose base units] of a product at the end of day d."""
    u = _fact("units", "unit conversions")
    names = find(u["product_name"], product, n=1)
    x = u[(u["product_name"] == names[0]) & (u["unit"] == fold(unit))] if names else u.iloc[0:0]
    if x.empty:
        raise Unanswerable(f"no conversion of {unit} for {product}")
    total, factor = stock(names[0], d), float(x["factor"].iloc[0])
    return [int(total // factor), int(round(total - factor * (total // factor)))]


def lots_left(d=None) -> pd.DataFrame:
    """Lots at the end of day d with stock left: lot, product_name, expiry, left (base units), price."""
    _fact("lots", "lots")
    _fact("lot_sales", "sales by lot")
    x = _facts.lot_stock(_FACTS, sales_lines(), _end(d))
    return x[x["left"] > 0]


# ---------------------------------------------------------------- answers

def _py(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (list, tuple, np.ndarray, pd.Series)):
        return [_py(x) for x in v]
    return v


def _num(x, kind: str):
    x = float(_py(x))
    if math.isnan(x) or math.isinf(x):
        raise ValueError("answer is NaN/inf")
    if kind in ("money", "count"):
        return int(round_half_up(x, 0))
    v = round_half_up(x, 2)
    return int(v) if v.is_integer() and kind == "auto" else v


def _set(r: dict) -> dict:
    global _RESULT
    _RESULT = r
    return r


def answer_number(x, kind: str = "auto") -> dict:
    """Final answer, type number. kind='money' (VND, rounded to đồng half-up), 'count', 'decimal' (2 dp) or 'auto'."""
    return _set({"type": "number", "answer": _num(x, kind)})


def answer_percent(x) -> dict:
    """Final answer, type percent, in percent units (12.4 means 12.4%), rounded to 2 dp."""
    return _set({"type": "percent", "answer": round_half_up(float(_py(x)), 2)})


def answer_string(s) -> dict:
    """Final answer, type string: a name copied exactly from the data."""
    return _set({"type": "string", "answer": str(_py(s))})


def answer_date(d) -> dict:
    """Final answer, type date 'YYYY-MM-DD'."""
    return _set({"type": "date", "answer": _d(d).strftime("%Y-%m-%d")})


def answer_month(y_or_ts, m: int | None = None) -> dict:
    """Final answer, type date for a month question: 'YYYY-MM'. Pass (year, month) or a Timestamp/Period."""
    if m is None:
        t = y_or_ts.start if isinstance(y_or_ts, Period) else pd.Timestamp(y_or_ts)
        y_or_ts, m = t.year, t.month
    return _set({"type": "date", "answer": f"{int(y_or_ts):04d}-{int(m):02d}"})


def answer_bool(b) -> dict:
    """Final answer, type boolean."""
    return _set({"type": "boolean", "answer": bool(_py(b))})


def answer_list(items, kind: str = "auto") -> dict:
    """Final answer, type list (ordered). Numbers are rounded like answer_number(kind=...)."""
    out = []
    for v in _py(list(items)):
        out.append(_num(v, kind) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
    return _set({"type": "list", "answer": out})


def answer_table(columns, rows, kind: str = "auto") -> dict:
    """Final answer, type table: first column = object name, then the measures in the order asked."""
    rr = []
    for r in _py(list(rows)):
        rr.append([_num(v, kind) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r])
    return _set({"type": "table", "answer": {"columns": [str(c) for c in columns], "rows": rr}})


def unanswerable(reason: str) -> dict:
    """Final answer when the released data cannot support the question."""
    return _set({"type": "unanswerable", "reason": str(reason), "_src": "model"})


__all__ = [
    "Period", "Unanswerable", "day", "today", "yesterday", "between", "until", "month", "month_auto",
    "this_month", "prev_month_same_days", "last_month", "prev_period", "same_period_last_year", "days_from", "days_before", "week_this",
    "week_prev", "last_n_days", "quarter",
    "quarter_auto", "year", "ytd", "tet", "pre_tet", "in_period", "require", "require_coverage", "shift_of",
    "count_weekday", "pct_change", "round_half_up", "rank_top", "find", "sales_lines", "orders", "revenue", "avg_order_value",
    "price_on", "is_walkin", "same_text", "debt_orders", "debt_total", "debt_customers", "debt_repaid", "debt_new", "delivery_trips", "imports",
    "avg_import_price", "gross_profit", "net_profit", "slow_products", "price_effect", "stock", "stock_split", "lots_left",
    "same_name", "fold", "norm_key",
    "answer_number", "answer_percent", "answer_string", "answer_date", "answer_month", "answer_bool",
    "answer_list", "answer_table", "unanswerable",
]


def api_doc() -> str:
    """One line per helper (signature + first docstring line), for the prompt."""
    lines = []
    for name in __all__:
        obj = globals()[name]
        if inspect.isclass(obj):
            lines.append(f"- {name}: {(obj.__doc__ or '').strip().splitlines()[0] if obj.__doc__ else ''}")
            continue
        sig = str(inspect.signature(obj)).replace("pandas.core.frame.", "").replace("pandas._libs.tslibs.timestamps.", "")
        doc = (inspect.getdoc(obj) or "").splitlines()
        lines.append(f"- {name}{sig}: {doc[0] if doc else ''}")
    return "\n".join(lines)
