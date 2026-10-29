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

from datamate.ingest.parsers import parse_date
from datamate.text import fold, norm_key

AS_OF: pd.Timestamp | None = None
CAL: pd.DataFrame | None = None
_RESULT: dict | None = None


class Unanswerable(Exception):
    """Raise (or call `unanswerable`) when the data cannot support the question."""


class Period(NamedTuple):
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def days(self) -> int:
        return int((self.end - self.start).days) + 1

    def __repr__(self) -> str:
        return f"Period({self.start:%Y-%m-%d} .. {self.end:%Y-%m-%d})"


def set_context(as_of, cal: pd.DataFrame | None) -> None:
    global AS_OF, CAL, _RESULT
    AS_OF = pd.Timestamp(as_of).normalize()
    CAL = cal
    _RESULT = None


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
    """Tháng trước / tháng rồi as a whole month."""
    s = (AS_OF.replace(day=1) - pd.Timedelta(days=1)).replace(day=1)
    return month(s.year, s.month)


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


def same_name(a, b) -> bool:
    """True if two names differ only in case, spacing, punctuation or diacritics."""
    return fold(norm_key(a)) == fold(norm_key(b))


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
    return _set({"type": "unanswerable", "reason": str(reason)})


__all__ = [
    "Period", "Unanswerable", "day", "today", "yesterday", "between", "until", "month", "month_auto",
    "this_month", "prev_month_same_days", "last_month", "week_this", "week_prev", "last_n_days", "quarter",
    "quarter_auto", "year", "ytd", "tet", "pre_tet", "in_period", "require", "require_coverage", "shift_of",
    "count_weekday", "pct_change", "round_half_up", "rank_top", "find", "same_name", "fold", "norm_key",
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
