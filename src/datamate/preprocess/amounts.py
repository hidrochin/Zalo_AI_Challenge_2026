"""Money amounts and compressed dates in questions: '3tr7' -> 3.700.000 đồng, '152k' -> 152.000 đồng,
'th7/26' -> 'tháng 7/2026', '3/25' -> 'tháng 3/2025', '6-2025' -> 'tháng 6/2025', 'quý 2/2025' -> 'quý 2 năm 2025'.

A bare 'a/bb' is read as a month only when bb is a plausible two-digit year (24-29): '3/25' cannot be a
day because there is no month 25. Everything else (days 'dd/mm', full dates) is left to agent/hints.py.
"""

import re

_NUM = r"(\d+(?:[.,]\d+)?)"
_TR_MIX = re.compile(r"(?<![\w.,/])(\d+)\s*(?:tr|triệu|trieu)\s*(\d{1,3})(?![\w.,])", re.I)
_MILLION = re.compile(r"(?<![\w.,/])" + _NUM + r"\s*(?:tr|triệu|trieu)(?!\w)", re.I)
_BILLION = re.compile(r"(?<![\w.,/])" + _NUM + r"\s*(?:tỷ|tỉ)(?![\w/]|\s*lệ)", re.I)
_THOUSAND = re.compile(r"(?<![\w.,/])" + _NUM + r"\s*(?:k|nghìn|ngàn|nghin|ngan)(?!\w)", re.I)

_QUARTER = re.compile(r"\b(?:quý|quy|q)\s*([1-4])\s*[/.-]\s*(\d{4}|\d{2})(?![\d/])", re.I)
_TH_MONTH = re.compile(r"(?<!\w)(?:tháng|thang|th|t)\s*(\d{1,2})\s*[/.-]\s*(\d{4}|\d{2})(?![\d/]|[.,-]\d)", re.I)
_BARE_MY4 = re.compile(r"(?<![\w/.,-])(\d{1,2})\s*[/-]\s*(20\d{2})(?![\d/]|[.,-]\d)")
_BARE_MY2 = re.compile(r"(?<![\w/.,-])(\d{1,2})/(2[4-9])(?![\d/]|[.,-]\d)")
_DMY2 = re.compile(r"(?<![\w/.])(\d{1,2})/(\d{1,2})/(\d{2})(?![\d/])")


def _vnd(x: float) -> str:
    return f"{int(round(x)):,}".replace(",", ".") + " đồng"


def _float(s: str, thousands_sep_ok: bool) -> float:
    """'1,5' / '1.5' are decimals; with thousands_sep_ok, '1.500' / '1,500' are 1500."""
    if thousands_sep_ok and re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return float(re.sub(r"[.,]", "", s))
    return float(s.replace(",", "."))


def normalize_amounts(text: str) -> tuple[str, list[str]]:
    """Rewrite slang money amounts as full VND; returns the text and notes like "'3tr7' = 3700000"."""
    notes: list[str] = []

    def sub(rx, fn):
        nonlocal text

        def rep(m):
            v = fn(m)
            notes.append(f"'{m.group(0).strip()}' = {int(round(v))} VND")
            return _vnd(v)

        text = rx.sub(rep, text)

    sub(_TR_MIX, lambda m: int(m.group(1)) * 1e6 + int(m.group(2)) * 10 ** (6 - len(m.group(2))))
    sub(_BILLION, lambda m: _float(m.group(1), False) * 1e9)
    sub(_MILLION, lambda m: _float(m.group(1), False) * 1e6)
    sub(_THOUSAND, lambda m: _float(m.group(1), True) * 1e3)
    return text, notes


def _year(y: str) -> str:
    return y if len(y) == 4 else "20" + y


def normalize_dates(text: str) -> str:
    text = _QUARTER.sub(lambda m: f"quý {m.group(1)} năm {_year(m.group(2))}", text)
    text = _TH_MONTH.sub(lambda m: f"tháng {int(m.group(1))}/{_year(m.group(2))}" if 1 <= int(m.group(1)) <= 12
                         else m.group(0), text)
    text = _DMY2.sub(lambda m: f"{m.group(1)}/{m.group(2)}/20{m.group(3)}", text)

    def bare(m):
        mm = int(m.group(1))
        if not 1 <= mm <= 12:
            return m.group(0)
        before = text[max(0, m.start() - 8):m.start()].lower()
        prefix = "" if re.search(r"(tháng|thang)\s*$", before) else "tháng "
        return f"{prefix}{mm}/{_year(m.group(2))}"

    for rx in (_BARE_MY4, _BARE_MY2):
        text = rx.sub(bare, text)
    return text
