"""Columns the header rules cannot place: one model call per table, saved next to the ingest cache.

Only asked when the shop misses a role sales need or yields no fact table (facts.py), and only about columns without a
role, so a shop the rules read in full never changes. A proposal counts only if the column's values pass ingest/trust.py
and, for facts, the table has the shape the fact needs.
"""

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pandas as pd

from datamate.canon import build_sales
from datamate.facts import build_facts, missing
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
# fact -> roles that make it; only the roles of facts the shop did not yield are offered
FACT_ROLES = {
    "orders": {"order_total": "money the customer must pay for the whole order, after the order's discount and with shipping",
               "pay_method": "how the order is paid: cash, transfer, on credit, cash on delivery", "channel": "where the order was placed: store, phone, an online marketplace"},
    "payments": {"pay_date": "date a payment was received for an order", "pay_amount": "money of one payment received for an order"},
    "deliveries": {"trip": "number of the delivery trip of an order (1, 2, 3)", "delivery_date": "date goods were delivered"},
    "imports": {"import_price": "price of one unit bought from a supplier", "import_qty": "quantity bought from a supplier", "import_doc": "code of the purchase receipt",
                "import_date": "date of the purchase receipt", "import_unit": "unit of the quantity bought"},
    "expenses": {"expense_amount": "money of one running cost of the shop", "expense_date": "date of that cost"},
    "stock": {"stock_balance": "quantity in stock", "stock_time": "date and time of a stock movement, on a stock card"},
    "lots": {"lot_id": "code of a batch", "lot_expiry": "expiry date of a batch", "lot_qty": "quantity that came in with a batch, in base units"},
    "lot_sales": {"lot_id": "code of a batch", "lot_sold": "quantity sold from a batch, in base units", "lot_returned": "quantity returned to a batch, in base units"},
    "cost_col": {"line_cost": "cost of one unit, written on the sale line"},
}
FACT_SYSTEM = ("You read one table from a Vietnamese shop's records and say which role each listed column plays. Roles:\n{roles}\n"
               "Reply with one JSON object mapping each column exactly as written to a role, or to null when it plays none or you are not sure. "
               "Most columns play none. Use each role at most once. Nothing else.")
IMPORT_ROLES = set(FACT_ROLES["imports"])
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


def parse_roles(reply: str, df, columns: list[str], taken: set[str], allowed=ROLES) -> dict[str, str]:
    """Column -> role for the columns asked about, without roles already used and values that do not fit."""
    m = _JSON.search(reply or "")
    try:
        got = json.loads(m.group(0)) if m else {}
    except ValueError:
        got = {}
    out: dict[str, str] = {}
    for c in columns:
        role = got.get(c) if isinstance(got, dict) else None
        if role in allowed and role not in taken and not role_problem(df, c, role):
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


def with_roles(biz: Business, roles: dict[str, dict[str, str]]) -> Business:
    """A view of the shop with these roles added; tables stay shared."""
    info = {n: replace(ti, roles={**ti.roles, **roles.get(n, {})}) for n, ti in biz.info.items()}
    return replace(biz, info=info)


def _is_order_total(df: pd.DataFrame, oid: str, col: str, sales: pd.DataFrame | None) -> bool:
    """One value per order, close to what the order's lines add up to (a discount or shipping moves it, a cost does not fit)."""
    rows = df.dropna(subset=[oid])
    if (rows.groupby(oid)[col].nunique() > 1).mean() > 0.05:
        return False
    if sales is None or not {"order_id", "amount"} <= set(sales.columns):
        return True
    lines = pd.to_numeric(sales["amount"], errors="coerce").groupby(sales["order_id"]).sum()
    total = pd.to_numeric(rows.drop_duplicates(oid).set_index(oid)[col], errors="coerce")
    ratio = (total / lines.reindex(total.index)).replace([float("inf"), -float("inf")], float("nan")).dropna()
    return ratio.empty or 0.5 <= ratio.median() <= 2


def _fit_table(biz: Business, name: str, found: dict[str, str], sales: pd.DataFrame | None) -> dict[str, str]:
    """The proposals this table's shape allows. A wrong fact gives a wrong number, a missing one only leaves the question to the model."""
    df, ti = biz.tables[name], biz.info[name]
    col_of = {r: c for c, r in {**ti.roles, **found}.items()}
    oid, lines = col_of.get("order_id"), "line_amount" in col_of
    product = col_of.get("product_id") or col_of.get("product_name")
    catalogue = product is not None and df[product].dropna().is_unique  # one row per product: no receipts, no movements

    def fits(col: str, role: str) -> bool:
        if role == "order_total":
            return oid is not None and _is_order_total(df, oid, col, sales)
        if role in ("pay_date", "pay_amount", "trip", "delivery_date"):
            return oid is not None and not lines
        if role in IMPORT_ROLES:
            return oid is None and not lines and not catalogue
        if role in ("expense_amount", "expense_date"):
            return oid is None and not lines and product is None
        if role == "stock_balance":
            return oid is None and not lines and not set(df[col].dropna().unique()) <= {0, 1, True, False}
        if role == "stock_time":
            return oid is None and not lines and not catalogue
        if role == "line_cost":
            price = col_of.get("unit_price")
            share = pd.to_numeric(df[col], errors="coerce") / pd.to_numeric(df[price], errors="coerce") if price else None
            return lines and (share is None or share.median() <= 1.2)
        return True

    return {c: r for c, r in found.items() if fits(c, r)}


def propose_fact_roles(biz: Business, generate: Callable[[str, list[str]], str]) -> dict[str, dict[str, str]]:
    """Roles of the facts this shop did not yield, one call per table with unplaced columns."""
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
    offered = {r: d for gap in missing(build_facts(biz, sales)) for r, d in FACT_ROLES.get(gap, {}).items()}
    if not offered:
        return {}
    system = FACT_SYSTEM.format(roles="\n".join(f"- {r}: {d}" for r, d in offered.items()))
    out = {}
    for name in biz.tables:
        cols = _free_columns(biz, name)
        if cols:
            found = parse_roles(generate(system, [table_part(name, biz)]), biz.tables[name], cols, set(biz.info[name].roles.values()), offered)
            found = _fit_table(biz, name, found, sales)
            if found:
                out[name] = found
    return out


def resolve_columns(biz: Business, cache_dir: Path, generate: Callable[[str, list[str]], str]) -> bool:
    """Ask once about this shop's unplaced columns. True when roles were saved; load the shop again to get them."""
    path = roles_path(cache_dir, biz.split, biz.id)
    if path.exists():
        return False
    roles = propose_roles(biz, generate)
    for name, found in propose_fact_roles(with_roles(biz, roles), generate).items():
        roles[name] = {**roles.get(name, {}), **found}
    path.write_text(json.dumps(roles, ensure_ascii=False, indent=1), encoding="utf-8")
    return bool(roles)
