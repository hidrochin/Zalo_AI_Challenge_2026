"""Deterministic question preprocessing: resolve Vietnamese time expressions to dm period calls.

The resolved calls are shown next to the question so the model copies them instead of inventing
periods ("tháng trước" after an explicit month means the month before THAT month, "hqua" is one
day, two-digit years, dates without a year, "N ngày kể từ ngày D", ...). Rulebook §2 semantics.
"""

import re
import threading

import pandas as pd

from datamate import dm
from datamate.text import fold

_LOCK = threading.Lock()  # dm keeps AS_OF in module globals

_Y = r"(\d{4}|\d{2})"
_DATE = re.compile(r"(?<![\d/])(\d{1,2})[/.-](\d{1,2})[/.-]" + _Y + r"(?![\d/])")
_DATE_NOYEAR = re.compile(r"(?:ngay|hom|bua|sang|chieu|toi)\s*(\d{1,2})[/.-](\d{1,2})(?![/.-]?\d)")
_MONTH_Y = re.compile(r"\b(?:thang|th|t)\s*(\d{1,2})\s*(?:[/.-]|\s+nam\s+|\s+)" + _Y + r"\b")
_MONTH_BARE_Y = re.compile(r"(?<![\d/.-])(\d{1,2})[/-](\d{4})\b")
_MONTH = re.compile(r"\b(?:thang|th)\s*(\d{1,2})\b(?!\s*(?:[/.-]|nam)\s*\d)")
_QUARTER = re.compile(r"\b(?:quy|q)\s*([1-4])\b(?:\s*(?:[/.-]|nam|\s)\s*(\d{4}))?")
_YEAR = re.compile(r"\bnam\s+(\d{4})\b")
_LAST_N = re.compile(r"\b(\d{1,3})\s*ngay\s*(?:gan (?:day|nhat)|qua|vua qua|vua roi|cuoi( cung)?|gan nhat)\b")
_FROM_D = re.compile(r"\b(\d{1,3})\s*ngay\s*(?:ke tu|tinh tu|bat dau tu|tu)\s*(?:ngay\s*)?(?:tang gia\s*)?")
_BEFORE_D = re.compile(r"\b(\d{1,3})\s*ngay\s*(?:ngay\s*)?(?:truoc do|lien truoc|ngay truoc|truoc|ngay lien truoc)\b")
_TET = re.compile(r"\btet\b(?:\s*(?:nam|am lich|nguyen dan)?\s*(\d{4}))?")

RELATIVE = [
    (re.compile(r"\b(hom nay|bua nay|ngay hom nay|hnay)\b"), "today()"),
    (re.compile(r"\b(hom qua|hqua|bua qua|hom wa|hum qua|qua hom|ngay hom qua)\b"), "yesterday()"),
    (re.compile(r"\btuan (nay|hien tai)\b"), "week_this()"),
    (re.compile(r"\btuan (truoc|roi|vua roi|vua qua|qua|trc)\b"), "week_prev()"),
    (re.compile(r"\btu dau nam\b|\bnam nay\b"), "ytd()"),
    (re.compile(r"\bnam (ngoai|truoc|roi|vua roi|qua|trc)\b"), "year({py})"),
]
_SAME_LAST_YEAR = re.compile(r"\bcung ky (nam|nam ngoai|nam truoc|nam trc|nam roi|nam ngoai)\b|\bcung thang (nam|nam ngoai|nam truoc|nam trc)\b")
_THIS_MONTH = re.compile(r"\bthang (nay|hien tai)\b|\btu dau thang\b")
_PREV_MONTH = re.compile(r"\b(?:thang|th) (truoc|roi|vua roi|vua qua|lien truoc|lien trc|trc|qua)\b|\bky truoc\b")
_UNTIL = re.compile(r"\b(den|toi|tinh den|tinh toi|cuoi|het)\s*(het\s*)?(ngay\s*)?(hom nay|nay)\b|\bhet hom nay\b|\bcuoi ngay hom nay\b")
_UNTIL_D = re.compile(r"\b(?:den|toi|tinh den|tinh toi|cuoi)\s*(?:het\s*)?(?:ngay\s*)?(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")


def _year(y: str) -> int:
    return int(y) + 2000 if len(y) == 2 else int(y)


def _ok_date(y, m, d) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(year=y, month=m, day=d)
    except ValueError:
        return None


def _fmt(p: dm.Period) -> str:
    return f"{p.start:%Y-%m-%d}..{p.end:%Y-%m-%d}"


def time_hints(question: str, as_of: str, cal: pd.DataFrame | None = None) -> list[str]:
    """Lines like '"tháng 5/2026" -> month(2026, 5) = 2026-05-01..2026-05-31'."""
    f = fold(question)
    out: list[tuple[int, str, str]] = []  # (position, phrase, call)
    taken: list[tuple[int, int]] = []

    def free(m) -> bool:
        return not any(a < m.end() and m.start() < b for a, b in taken)

    def add(m, call: str):
        taken.append((m.start(), m.end()))
        out.append((m.start(), m.group(0).strip(), call))

    with _LOCK:
        dm.set_context(as_of, cal)
        t = dm.AS_OF
        anchor = None  # last explicit period, for "tháng trước" / "kỳ trước"
        until_spans = [(m.start(), m.end()) for m in _UNTIL_D.finditer(f)]
        for m in _DATE.finditer(f):
            d = _ok_date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
            if d is not None:
                inside_until = any(a <= m.start() < b for a, b in until_spans)
                add(m, f"until('{d:%Y-%m-%d}')" if inside_until else f"day('{d:%Y-%m-%d}')")
        for m in _DATE_NOYEAR.finditer(f):
            if not free(m):
                continue
            dd, mm = int(m.group(1)), int(m.group(2))
            d = _ok_date(t.year, mm, dd)
            if d is not None and d > t:
                d = _ok_date(t.year - 1, mm, dd)
            if d is not None:
                add(m, f"day('{d:%Y-%m-%d}')")
        for rx in (_MONTH_Y, _MONTH_BARE_Y):
            for m in rx.finditer(f):
                mm, yy = int(m.group(1)), _year(m.group(2))
                if free(m) and 1 <= mm <= 12 and 2000 < yy < 2100:
                    add(m, f"month({yy}, {mm})")
                    anchor = anchor or f"month({yy}, {mm})"
        for m in _MONTH.finditer(f):
            mm = int(m.group(1))
            if free(m) and 1 <= mm <= 12:
                p = dm.month_auto(mm)
                add(m, f"month({p.start.year}, {mm})")
                anchor = anchor or f"month({p.start.year}, {mm})"
        for m in _QUARTER.finditer(f):
            if not free(m):
                continue
            q = int(m.group(1))
            yy = int(m.group(2)) if m.group(2) else dm.quarter_auto(q).start.year
            add(m, f"quarter({yy}, {q})")
            anchor = anchor or f"quarter({yy}, {q})"
        for m in _YEAR.finditer(f):
            if free(m):
                add(m, f"year({m.group(1)})")
                anchor = anchor or f"year({m.group(1)})"
        for m in _LAST_N.finditer(f):
            if free(m):
                add(m, f"last_n_days({int(m.group(1))})")
        for m in _TET.finditer(f):
            if free(m):
                yy = int(m.group(1)) if m.group(1) else (t.year if t.month <= 3 else t.year + 1)
                if "truoc tet" in f:
                    out.append((m.start(), "trước Tết", f"pre_tet({yy})"))
                add(m, f"tet({yy})")
        days = [c for _, _, c in out if c.startswith("day(")]
        if days:  # "N ngày kể từ ngày D" / "N ngày ngay trước đó"
            d0 = days[0][4:-1]
            for m in _FROM_D.finditer(f):
                out.append((m.start(), m.group(0).strip(), f"days_from({d0}, {int(m.group(1))})"))
            for m in _BEFORE_D.finditer(f):
                if free(m):
                    add(m, f"days_before({d0}, {int(m.group(1))})")
        range_end = re.search(r"\btu dau\b|\btu ngay\b|\bthang nay\b|\btuan nay\b", f)
        for m in _UNTIL.finditer(f):
            if free(m) and not range_end:
                add(m, "until()  # point-in-time: everything from the start of the data to the end of today")
        this_m = _THIS_MONTH.search(f)
        if this_m and free(this_m):
            add(this_m, "this_month()")
        ly = _SAME_LAST_YEAR.search(f)
        if ly and free(ly):
            add(ly, f"same_period_last_year({anchor or 'this_month()'})")
        pm = _PREV_MONTH.search(f)
        if pm and free(pm):
            if this_m:
                add(pm, "prev_month_same_days()  # 'tháng này' vs the same days of last month")
            elif anchor:
                add(pm, f"prev_period({anchor})  # the period just before {anchor}, NOT relative to today")
            else:
                add(pm, "last_month()")
        for rx, call in RELATIVE:
            m = rx.search(f)
            if m and free(m):
                add(m, call.format(py=t.year - 1))

        lines, seen = [], set()
        for _, phrase, call in sorted(out):
            if call in seen:
                continue
            seen.add(call)
            expr = call.split("  #")[0]
            try:
                p = eval(expr, {n: getattr(dm, n) for n in dm.__all__})
                rng = f" = {_fmt(p)}" if isinstance(p, dm.Period) else ""
                rng = rng.replace("1900-01-01..", "start of data..")
            except Exception:
                continue
            lines.append(f'- "{phrase}" -> {call.split("  #")[0]}{rng}' + (f"  ({call.split('  # ')[1]})" if "  # " in call else ""))
    return lines
