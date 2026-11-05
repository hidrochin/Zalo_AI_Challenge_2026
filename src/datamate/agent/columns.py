"""Columns the header rules cannot place: one model call per table, saved next to the ingest cache.

Only asked when the shop misses a role sales need, and only about columns without a role, so a shop the rules read
in full never changes. A proposal counts only if the column's values pass ingest/trust.py.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

from datamate.canon import build_sales
from datamate.ingest.business import META_COLS, Business, roles_path
from datamate.ingest.parsers import _is_null
from datamate.ingest.trust import role_problem

ROLES = {
    "order_id": "code of the order or bill; rows of one order share it",
    "order_date": "date of the order or sale",
    "order_ts": "date and time the order was created",
    "status": "state of the order: finished, cancelled, returned, being delivered",
    "product_id": "code of the product sold",
    "product_name": "name of the product or dish sold",
    "qty": "quantity sold, in the unit written on the line",
    "qty_base": "quantity converted to the product's base unit",
    "unit": "unit of the quantity",
    "unit_price": "price of one unit",
    "line_amount": "money of this sale line, after the line's discount",
    "customer_id": "code of the customer",
    "customer_name": "name of the customer",
}
SYSTEM = ("You read one table from a Vietnamese shop's records and say which sales role each listed column plays. Roles:\n"
          + "\n".join(f"- {r}: {d}" for r, d in ROLES.items())
          + "\nA role describes a SALE to a customer: a column about purchases from suppliers, stock, staff, payments or debts has no role. "
            "Reply with one JSON object mapping each column exactly as written to a role, or to null when it plays none or you are not sure. "
            "Use each role at most once. Nothing else.")
NEEDED = ({"order_date", "order_ts"}, {"line_amount"}, {"product_id", "product_name"}, {"qty", "qty_base"})
_JSON = re.compile(r"\{.*\}", re.S)


def _samples(col) -> str:
    vals = [str(v)[:40] for v in col.dropna().unique()[:5] if not _is_null(v)]
    return " | ".join(vals) or "(empty)"


def table_part(name: str, biz: Business) -> str:
    df, ti = biz.tables[name], biz.info[name]
    known = ", ".join(f"{c} = {r}" for c, r in ti.roles.items()) or "none"
    lines = [f"Table: {name} ({len(df)} rows). Columns already known: {known}", "Columns to place:"]
    for c in _free_columns(biz, name):
        desc = f" (README: {ti.descs[c]})" if c in ti.descs else ""
        lines.append(f"- {c}{desc}: {_samples(df[c])}")
    return "\n".join(lines)


def _free_columns(biz: Business, name: str) -> list[str]:
    ti = biz.info[name]
    return [c for c in biz.tables[name].columns if c not in ti.roles and c not in META_COLS and not c.endswith(("__norm", "__unit"))]


def tables_to_map(biz: Business) -> list[str]:
    """Every table when sales cannot be built in full, the order tables when only the status is missing, else nothing.
    A shop without a status role would count its cancelled orders as sales."""
    have = {r for ti in biz.info.values() for r in ti.roles.values()}
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
    if sales is None or not all(need & have for need in NEEDED):
        return [n for n in biz.tables if _free_columns(biz, n)]
    if "status" in have:
        return []
    return [n for n, ti in biz.info.items() if {"line_amount", "order_id"} & set(ti.roles.values()) and _free_columns(biz, n)]


def parse_roles(reply: str, df, columns: list[str], taken: set[str]) -> dict[str, str]:
    """Column -> role for the columns asked about, without roles already used and values that do not fit."""
    m = _JSON.search(reply or "")
    try:
        got = json.loads(m.group(0)) if m else {}
    except ValueError:
        got = {}
    out: dict[str, str] = {}
    for c in columns:
        role = got.get(c) if isinstance(got, dict) else None
        if role in ROLES and role not in taken and not role_problem(df, c, role):
            out[c] = role
            taken = taken | {role}
    return out


def _line_amounts_on_lines(biz: Business, proposed: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """A money column of a table without products is an order total when another table holds the product lines."""
    roles = {n: set(ti.roles.values()) | set(proposed.get(n, {}).values()) for n, ti in biz.info.items()}
    sold = {"product_id", "product_name"}
    if not any("line_amount" in r and sold & r for r in roles.values()):
        return proposed
    return {n: {c: r for c, r in cols.items() if r != "line_amount" or sold & roles[n]} for n, cols in proposed.items()}


def propose_roles(biz: Business, generate: Callable[[str, list[str]], str]) -> dict[str, dict[str, str]]:
    out = {}
    for name in tables_to_map(biz):
        cols = _free_columns(biz, name)
        roles = parse_roles(generate(SYSTEM, [table_part(name, biz)]), biz.tables[name], cols, set(biz.info[name].roles.values()))
        if roles:
            out[name] = roles
    return {n: r for n, r in _line_amounts_on_lines(biz, out).items() if r}


def resolve_columns(biz: Business, cache_dir: Path, generate: Callable[[str, list[str]], str]) -> bool:
    """Ask once about this shop's unplaced columns. True when roles were saved; load the shop again to get them."""
    path = roles_path(cache_dir, biz.split, biz.id)
    if path.exists():
        return False
    roles = propose_roles(biz, generate)
    path.write_text(json.dumps(roles, ensure_ascii=False, indent=1), encoding="utf-8")
    return bool(roles)
