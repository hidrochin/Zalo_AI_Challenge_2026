"""Answer post-processing: what the question expects, checks against it, and the exact output format.

expect_answer()  reads the answer type (and top-k, day vs month) off the rule-normalized question. Only cue
                 patterns that are ≥ 99% precise on train gold are `hard`; the LLM rewrite's guess is `soft`.
check_answer()   a message for the model when an executed answer contradicts the expectation.
finalize()       the submission line: every type in the official format, anything malformed -> abstain.
"""

import json
import math
import re
from dataclasses import dataclass

from datamate.text import fold

TYPES = ("number", "percent", "string", "date", "boolean", "list", "table")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")

# (type, month?, pattern on the folded rule-normalized question); checked in order, first match wins
HARD_RULES: list[tuple[str, bool | None, re.Pattern]] = [
    ("date", True, re.compile(r"\bthang nao\b(?!.*\bngay nao\b)")),
    ("date", False, re.compile(r"\bngay nao\b|\bhan (dung|su dung) (la )?ngay\b|\bhet han (vao )?ngay nao\b")),
    # '%' right after a number is part of a name or condition ('Efticol 0,9%'), not the asked unit
    ("percent", None, re.compile(r"(?<![\d\s])\s*%|\bphan tram\b|\bti le\b|\bty le\b")),
    ("table", None, re.compile(r"\bkem (theo )?so (tien|no)\b|\bmoi (nguoi|khach) (con )?no\b|\bso no cua tung\b")),
    ("list", None, re.compile(r"\btop \d+\b|\blan luot\b|\btheo thu tu\b|\btuoi no\b")),
]
_K = re.compile(r"\btop (\d+)\b|\b(\d+) (?:mat hang|mon|san pham|khach hang|khach|loai|nguoi|nhan vien|thuoc|nha cung cap|"
                r"hang|ma|sp|tho|nhom)\b(?! (?:tro|tu|tro len))")


@dataclass
class Expect:
    type: str | None = None
    month: bool | None = None  # date answers: True = 'YYYY-MM', False = 'YYYY-MM-DD'
    k: int | None = None       # top-k: a list/table answer has at most k items
    hard: bool = False         # type/month expectation from a ≥ 99%-precise rule
    k_hard: bool = False       # k read off the question by rule (train: 120/120), not guessed by the rewrite
    source: str = ""
    window: tuple[str, str] | None = None  # a date answer lies in [start, end] of the periods the question names


_RANGE = re.compile(r"= (\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")
_EXPIRY = re.compile(r"\bhan (dung|su dung)\b|\bhet han\b|\bhsd\b")


def date_window(rules_text: str, hints: list[str]) -> tuple[str, str] | None:
    """Envelope of the resolved periods ('từ tháng 9 đến tháng 11' -> 09-01..11-30). None for expiry questions,
    whose answer (an expiry date) lies after the period. Gold date answers on train: 141/141 inside."""
    if _EXPIRY.search(fold(rules_text)):
        return None
    ps = [m.groups() for h in hints for m in [_RANGE.search(h)] if m]
    return (min(a for a, _ in ps), max(b for _, b in ps)) if ps else None


def expect_answer(rules_text: str, llm: dict | None = None) -> Expect:
    f = fold(rules_text)
    e = Expect()
    for t, month, rx in HARD_RULES:
        if rx.search(f):
            e.type, e.month, e.hard, e.source = t, month, True, f"rule: {rx.pattern[:40]}"
            break
    m = _K.search(f)
    if m and e.type in (None, "list", "table"):
        k = int(m.group(1) or m.group(2))
        if 2 <= k <= 20 and not re.search(r"\b" + str(k) + r" (?:ngay|thang|tuan|nam|don|lan)\b", f):
            e.k, e.k_hard = k, True
    lt = (llm or {}).get("answer_type")
    if lt == "month":
        lt, lmonth = "date", True
    else:
        lmonth = False if lt == "date" else None
    if lt in TYPES:
        if e.type is None:
            e.type, e.month, e.source = lt, lmonth, "llm"
        elif e.type != lt:  # rule and rewrite disagree: keep the rule, but only as a soft expectation
            e.hard, e.source = False, e.source + f" (llm says {lt})"
    lk = (llm or {}).get("top_k")
    if e.k is None and isinstance(lk, int) and not isinstance(lk, bool) and 2 <= lk <= 20 and e.type in ("list", "table"):
        e.k = lk
    return e


def fit_month(result: dict, e: Expect) -> dict:
    """A month asked ('tháng nào') and answered by its first day ('2025-10-01', what date_trunc gives): that month."""
    a = result.get("answer")
    if e.month and result.get("type") == "date" and isinstance(a, str) and _DAY.match(a) and a.endswith("-01"):
        return {**result, "answer": a[:7]}
    return result


_MONEY_WORD = re.compile(r"\b(tien|doanh thu|doanh so|gia tri|gia von|tien von|lai|loi nhuan|no|chi phi|dong|vnd|von)\b")
_NOT_MONEY = re.compile(r"\b(bao nhieu|may|so|dem)\s+(don|khach|lan|ngay|mat hang|mon|san pham|hoa don|phieu|nguoi|ky|chuyen|"
                        r"dot|luot|xe|ly|goi|lon|chai|cai|hop|vien|bich|may|chiec)\b|\bluot\b|\bphan tram\b|%")


def fit_money(result: dict, rules_text: str) -> dict:
    """Rulebook: money is whole đồng, an average of money too, half up. A fractional number asked about money is rounded
    (never a non-integer gold among 1427 money-like train and public questions)."""
    a = result.get("answer")
    if result.get("type") != "number" or isinstance(a, bool) or not isinstance(a, float) or a == int(a):
        return result
    f = fold(rules_text)
    if not _MONEY_WORD.search(f) or _NOT_MONEY.search(f):
        return result
    return {**result, "answer": int(math.floor(a + 0.5))}


def check_answer(result: dict, e: Expect) -> tuple[str | None, bool]:
    """(message for one re-check round, hard) when the executed answer contradicts the expectation, else (None, False).
    A hard contradiction that survives the re-check makes the question abstain. A type that only the LLM rewrite expects
    is no contradiction."""
    t, a = result.get("type"), result.get("answer")
    if t == "unanswerable":
        return None, False
    if e.type and t != e.type and not e.hard:  # only the rewrite's guess: asking again turned right answers wrong (train: net -11)
        return None, False
    if e.type and t != e.type:
        want = {"date": "answer_month(...) ('YYYY-MM')" if e.month else "answer_date(...) ('YYYY-MM-DD')"}.get(
            e.type, f"answer_{'bool' if e.type == 'boolean' else e.type}(...)")
        return (f"The question asks for a {e.type}{' (month)' if e.month else ''} answer, but the program returned "
                f"type {t!r}. Re-read the question; if it really asks for a {e.type}, end with {want}."), e.hard
    if t == "date" and e.month is not None and isinstance(a, str):
        if e.month and not _MONTH.match(a):
            return "The question asks which MONTH ('tháng nào'): answer with answer_month(year, month) -> 'YYYY-MM'.", e.hard
        if e.month is False and not _DAY.match(a):
            return "The question asks which DAY ('ngày nào'): answer with answer_date(...) -> 'YYYY-MM-DD'.", e.hard
    if t == "date" and e.window and isinstance(a, str) and (_DAY.match(a) or _MONTH.match(a)):
        lo, hi = (a + "-01", a + "-31") if _MONTH.match(a) else (a, a)
        if hi < e.window[0] or lo > e.window[1]:
            return (f"The answer {a} lies outside the period the question asks about ({e.window[0]}..{e.window[1]}). "
                    "Filter the rows with in_period(...) on that period before ranking."), True
    if e.k and t in ("list", "table"):
        n = len(a) if t == "list" else len(a.get("rows", []))
        if n > e.k:
            return (f"The question asks for the top {e.k}, but the answer has {n} items. "
                    f"Return at most {e.k} (fewer only if fewer exist)."), e.k_hard
    return None, False


# ---------------------------------------------------------------- output format

def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"not a finite number: {v!r}")
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return int(round(v))
    return v


def _cell(v):
    if isinstance(v, str):
        s = " ".join(v.split())
        if not s:
            raise ValueError("empty string")
        return s
    return _num(v)


def finalize(final: dict) -> dict:
    """The submission object for one question (without id). Malformed answers become abstain."""
    t = final.get("type")
    try:
        if t == "abstain":
            return {"type": "abstain"}
        if t == "unanswerable":
            return {"type": "unanswerable", "reason": str(final.get("reason") or "not answerable from the data")[:500]}
        a = final.get("answer")
        if t == "number":
            out = _num(a)
        elif t == "percent":
            out = round(float(_num(a)), 2) + 0.0  # + 0.0 turns -0.0 into 0.0
        elif t == "string":
            if not isinstance(a, str):
                raise ValueError("string answer is not a str")
            out = _cell(a)
        elif t == "date":
            if not isinstance(a, str) or not (_DAY.match(a) or _MONTH.match(a)):
                raise ValueError(f"bad date {a!r}")
            out = a
        elif t == "boolean":
            if not isinstance(a, bool):
                raise ValueError("boolean answer is not a bool")
            out = a
        elif t == "list":
            if not isinstance(a, list) or not a:
                raise ValueError("empty or non-list answer")
            out = [_cell(v) for v in a]
        elif t == "table":
            cols, rows = (a or {}).get("columns"), (a or {}).get("rows")
            if not isinstance(cols, list) or not cols or not isinstance(rows, list) or not rows:
                raise ValueError("table needs columns and rows")
            if (len(rows) == len(cols) and all(isinstance(r, list) for r in rows)
                    and len({len(r) for r in rows}) == 1 and len(rows[0]) not in (0, len(cols))):
                rows = [list(r) for r in zip(*rows)]  # column-major [[names...], [amounts...]] -> one row per object
            if any(not isinstance(r, list) or len(r) != len(cols) for r in rows):
                raise ValueError("table rows must have one cell per column")
            out = {"columns": [str(c) for c in cols], "rows": [[_cell(v) for v in r] for r in rows]}
        else:
            raise ValueError(f"unknown type {t!r}")
        json.dumps(out, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {"type": "abstain"}
    return {"type": t, "answer": out}


def validate_submission(lines: list[dict], ids: list[str]) -> dict:
    """Format report for a submission: duplicate / unknown / missing ids and lines finalize() would change."""
    seen, dup, bad = set(), [], []
    for ln in lines:
        i = ln.get("id")
        if i in seen:
            dup.append(i)
        seen.add(i)
        body = {k: v for k, v in ln.items() if k != "id"}
        ok = isinstance(body.get("reason"), str) if body.get("type") == "unanswerable" else finalize(body) == body
        if not ok:
            bad.append(i)
    known = set(ids)
    return {"lines": len(lines), "duplicates": dup, "unknown_ids": sorted(seen - known),
            "missing_ids": len(known - seen), "malformed": bad}


# ---------------------------------------------------------------- invariants (no data needed)

_RATE = re.compile(r"\bti le\b|\bty le\b|\bchiem\b|\bti trong\b|\bty trong\b|\bdong gop\b|\bphan cua\b|(%|\bphan tram) don\b")
_CHANGE = re.compile(r"\btang\b|\bgiam\b|\bthay doi\b|\bso (voi|vs)\b|\bchenh\b|\bhon\b|\bkem\b|\blen\b|\bxuong\b")
_COUNT = re.compile(r"\b(bao nhieu|may|so|dem)\s+(don|khach|lan|ngay|mat hang|mon|san pham|hoa don|phieu|nguoi|ky|chuyen|dot)\b")
_MONEY = re.compile(r"\bdoanh thu\b|\bdoanh so\b|\bbao nhieu tien\b|\bso tien\b|\btien hang\b")
_AMOUNT = re.compile(r"\d[\d.,]*\s*(d|dong|k|nghin|trieu|tr|cu)\b|\b\d{1,3}(\.\d{3})+\b|\b\d{5,}\b")  # 'từ 760.000đ': money is the filter
_SIGNED = re.compile(r"\blai\b|\bloi nhuan\b|\bchenh\b|\blo\b|\bam\b|\btru\b|\bhieu\b")
_WALKIN_NAME = re.compile(r"^(khach le|khach vang lai|vang lai|le|khach)$")


def invariants(result: dict, rules_text: str, window: tuple[str, str] | None = None) -> list[tuple[str, str]]:
    """(name, message) for every business invariant the answer breaks. Each one must never fire on train gold
    (see `datamate eval-checks`)."""
    f = fold(rules_text)
    t, a = result.get("type"), result.get("answer")
    out = []
    num = isinstance(a, (int, float)) and not isinstance(a, bool)
    if t == "percent" and num and _RATE.search(f) and not _CHANGE.search(f) and not 0 <= a <= 100:
        out.append(("rate_range", f"A share/rate must lie in 0..100, got {a}."))
    if t == "number" and num and _COUNT.search(f) and not _MONEY.search(f):
        if a < 0 or a != int(a):
            out.append(("count_int", f"A count must be a non-negative integer, got {a}."))
        elif window and re.search(r"\b(bao nhieu|may|so) ngay\b", f):
            days = (_days(window[1]) - _days(window[0])) + 1
            if a > days:
                out.append(("days_le_period", f"{a} days is more than the {days} days of the period asked."))
    if t == "number" and num and a < 0 and not _SIGNED.search(f):
        out.append(("negative", f"{a} is negative, but the question asks for an amount or count that cannot be."))
    if t == "number" and num and _MONEY.search(f) and not _COUNT.search(f) and not _AMOUNT.search(f) and 0 < abs(a) < 1000:
        out.append(("money_unit", f"{a} đồng is implausibly small for a VND amount; check units (VND, not thousands)."))
    names = [a] if t == "string" else a if t == "list" else [r[0] for r in (a or {}).get("rows", []) if r] if t == "table" else []
    if any(isinstance(v, str) and _WALKIN_NAME.match(fold(v)) for v in names):
        out.append(("walkin", "Walk-in customers (khách lẻ) are excluded from customer rankings (rulebook)."))
    return out


def _days(d: str) -> int:
    import datetime as _dt

    return _dt.date.fromisoformat(d).toordinal()
