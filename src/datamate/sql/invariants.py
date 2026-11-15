"""Invariants of the rule macros that need no gold answer (L1, L10): a macro that breaks one is wrong on this store.

Hand-written macros: revenue adds up over days, a cancel rate lies in 0..100, cancelled orders are a subset, debt is never
negative. Compiled macros (sql/compile.py): the same call gives the same value, a period after the data gives NULL or 0,
and a rate or share lies in 0..100. `datamate rule-check` runs them on every store of a split; the compiler sends a
violation back to the model like an error.
"""

import re
from datetime import date, timedelta

import duckdb

_RATE = re.compile(r"rate|share|ratio|ti le|ty le|phan tram|percent|%", re.I)
_CHANGE = re.compile(r"change|thay doi|tang|giam|pct|diff|chenh", re.I)


def _one(con, sql: str):
    try:
        return con.execute(sql).fetchone()[0]
    except duckdb.Error:
        return "error"


def _has(con, name: str) -> bool:
    return bool(con.execute("select count(*) from duckdb_functions() where function_name = ?", [name]).fetchone()[0])


def hand_problems(con, end: date) -> list[str]:
    """Violations of the hand-written macros on the store behind `con` (as_of set to the data end)."""
    out = []
    a, b = end - timedelta(days=6), end
    d = lambda x: f"date '{x}'"  # noqa: E731
    if _has(con, "revenue"):
        whole = _one(con, f"select revenue({d(a)}, {d(b)})")
        days = [_one(con, f"select revenue({d(a + timedelta(days=i))}, {d(a + timedelta(days=i))})") for i in range(7)]
        if "error" not in [whole, *days] and abs((whole or 0) - sum(x or 0 for x in days)) > 0.5:
            out.append(f"revenue({a}..{b}) = {whole} but its days add up to {sum(x or 0 for x in days)}")
    if _has(con, "cancel_rate"):
        r = _one(con, f"select cancel_rate({d(end - timedelta(days=29))}, {d(end)})")
        if r not in (None, "error") and not 0 <= r <= 100:
            out.append(f"cancel_rate = {r}, outside 0..100")
    if _has(con, "cancel_count") and _has(con, "orders"):
        c = _one(con, f"select cancel_count({d(end - timedelta(days=29))}, {d(end)})")
        n = _one(con, f'select count(*) from orders where "date" between {d(end - timedelta(days=29))} and {d(end)}')
        if "error" not in (c, n) and c is not None and c > n:
            out.append(f"cancel_count {c} above the {n} orders of the period")
    if _has(con, "debt_total"):
        t = _one(con, f"select debt_total({d(end)})")
        if t not in (None, "error") and t < -0.5:
            out.append(f"debt_total = {t} < 0")
    return out


def compiled_problem(con, macro, end: date) -> str | None:
    """A violation of a compiled scalar macro over a period (two date arguments), or None."""
    if macro.table or len(macro.args) != 2 or any(macro.types.get(x, "date") != "date" for x in macro.args):
        return None
    call = lambda a, b: f"select {macro.name}(date '{a}', date '{b}')"  # noqa: E731
    first = _one(con, call(end - timedelta(days=29), end))
    if first == "error":
        return None  # try_macro reports errors
    if _one(con, call(end - timedelta(days=29), end)) != first:
        return f"{macro.name} gives two values for the same call"
    later = _one(con, call(end + timedelta(days=400), end + timedelta(days=430)))
    if later not in (None, 0, 0.0, "error"):
        return f"{macro.name} gives {later} for a period after the data, where nothing happened (expected NULL or 0)"
    if _RATE.search(f"{macro.name} {macro.doc}") and not _CHANGE.search(f"{macro.name} {macro.doc}") and isinstance(first, (int, float)) \
            and not 0 <= first <= 100:
        return f"{macro.name} is a rate or share but gives {first}, outside 0..100"
    return None
