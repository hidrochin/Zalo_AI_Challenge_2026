"""Parsers for messy cell values: money, numbers with units, mixed-format dates."""

import datetime as dt
import math
import re

import pandas as pd

_MULT = {"tr": 1e6, "triệu": 1e6, "trieu": 1e6, "k": 1e3, "nghìn": 1e3, "ngàn": 1e3, "ng": 1e3}
_CURRENCY = {"đ", "d", "vnd", "vnđ", "₫", "%"}
_NUM_RE = re.compile(r"^([+-]?[\d.,]*\d)(tr|triệu|trieu|k|nghìn|ngàn|ng|đ|vnd|vnđ|₫|%)?\.?$")
_TR_MID_RE = re.compile(r"^(\d{1,4})(?:tr|triệu|trieu)(\d{1,3})(?:đ|vnd|vnđ|₫)?$")
_QTY_UNIT_RE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s+([^\d\s].{0,20})$")

_EXCEL_EPOCH = pd.Timestamp("1899-12-30")
_SERIAL_MIN, _SERIAL_MAX = 20000, 80000  # ~1954 .. ~2119


def _is_null(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if v is pd.NaT:
        return True
    return isinstance(v, str) and not v.strip()


def _digits_to_float(d: str, has_mult: bool) -> float:
    neg = d.startswith("-")
    d = d.lstrip("+-")
    if "," in d and "." in d:
        dec = "," if d.rfind(",") > d.rfind(".") else "."
        d = d.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in d or "." in d:
        sep = "," if "," in d else "."
        parts = d.split(sep)
        if len(parts) > 2:
            d = d.replace(sep, "")
        elif has_mult:
            d = d.replace(sep, ".")  # 1,6256tr / 1.5k
        elif len(parts[1]) == 3 and parts[0] not in ("", "0"):
            d = d.replace(sep, "")  # 55,000 / 422.400
        else:
            d = d.replace(sep, ".")
    x = float(d)
    return -x if neg else x


def parse_number(v):
    """Parse '55,000', '55.000đ', '55k', '1,6256tr', '45,000.0000', '3 chai' -> float, else None."""
    if _is_null(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, pd.Timestamp)):
        return None
    s = str(v).strip().lower().replace("\xa0", "")
    m = _TR_MID_RE.match(s.replace(" ", ""))
    if m:  # '1tr134' = 1.134 triệu, '2tr5' = 2.5 triệu
        return int(m.group(1)) * 1e6 + int(m.group(2).ljust(3, "0")) * 1e3
    m = _NUM_RE.match(s.replace(" ", ""))
    if m:
        suffix = m.group(2)
        try:
            x = _digits_to_float(m.group(1), suffix in _MULT)
        except ValueError:
            return None
        return x * _MULT.get(suffix, 1.0)
    m = _QTY_UNIT_RE.match(s)
    if m and m.group(2).strip() not in _CURRENCY:
        try:
            return _digits_to_float(m.group(1).replace(",", "."), True)
        except ValueError:
            return None
    return None


def parse_unit(v):
    """Unit embedded in a quantity cell ('3 chai' -> 'chai'), else None."""
    if _is_null(v) or not isinstance(v, str):
        return None
    m = _QTY_UNIT_RE.match(v.strip())
    return m.group(2).strip() if m and m.group(2).strip().lower() not in _CURRENCY else None


_DMY = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_DM = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})$")
_ISO = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?)?$")
_THG = re.compile(r"^(\d{1,2})\s*(?:thg|tháng|th)\s*(\d{1,2})\s*,?\s*(\d{4})$")
_SERIAL_STR = re.compile(r"^\d{5}(\.\d+)?$")


def _mk(y, m, d, hh=0, mm=0, ss=0):
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return pd.Timestamp(year=y, month=int(m), day=int(d), hour=int(hh or 0), minute=int(mm or 0), second=int(ss or 0))
    except ValueError:
        return None


def parse_date(v, year_hint: int | None = None, allow_serial: bool = True):
    """Parse dd/mm/yyyy, ISO, Excel serials, '1 thg 1 2025', datetimes, 'dd/mm' (+year_hint)."""
    if _is_null(v) or isinstance(v, bool):
        return None
    if isinstance(v, pd.Timestamp):
        return v
    if isinstance(v, (dt.datetime, dt.date)):
        return pd.Timestamp(v)
    if isinstance(v, (int, float)):
        if allow_serial and _SERIAL_MIN <= v <= _SERIAL_MAX:
            return _EXCEL_EPOCH + pd.to_timedelta(float(v), unit="D")
        return None
    s = str(v).strip()
    m = _DMY.match(s)
    if m:
        return _mk(m.group(3), m.group(2), m.group(1), m.group(4), m.group(5), m.group(6))
    if _ISO.match(s):
        try:
            return pd.Timestamp(s)
        except ValueError:
            return None
    m = _THG.match(s.lower())
    if m:
        return _mk(m.group(3), m.group(2), m.group(1))
    m = _DM.match(s)
    if m and year_hint:
        return _mk(year_hint, m.group(2), m.group(1))
    if allow_serial and _SERIAL_STR.match(s):
        return parse_date(float(s), allow_serial=True)
    return None


def is_explicit_date(v) -> bool:
    """True for values that are unambiguously dates (not bare serial numbers)."""
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return True
    return isinstance(v, str) and parse_date(v, year_hint=2000, allow_serial=False) is not None


_YEAR = re.compile(r"(20\d{2})")
_MONTH_BEFORE_YEAR = re.compile(r"(?<!\d)(\d{1,2})\D{1,3}20\d{2}")


def year_month_hint(*names: str) -> tuple[int | None, int | None]:
    """Year/month from a sheet or file name: 'T01_2025', '04.2025', 'Thang 02 2025', 'SoBanHang_2026'."""
    for name in names:
        if not name:
            continue
        y = _YEAR.search(name)
        if y:
            m = _MONTH_BEFORE_YEAR.search(name)
            month = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else None
            return int(y.group(1)), month
    return None, None
