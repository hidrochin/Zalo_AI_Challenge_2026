"""One read-only DuckDB database per business, built once and cached.

Tables: `sales_all`, `orders_all`, `dim_product`, `dim_customer`, `calendar` and every cleaned source table as
`raw_<name>`.
Views `sales_lines` and `orders` cut at the session variable `as_of`, so a query cannot see records that have not
happened yet; `counted` and `status` are computed on the full data first, because an order's cancel or return status
holds even when it is dated after as-of. Macros are the semantic layer, and exist only where the shop has the data.
"""

import hashlib
import re
import threading
from pathlib import Path

import duckdb
import pandas as pd

from datamate.canon import build_orders, build_sales
from datamate.facts import Facts, build_facts
from datamate.ingest import load_business, load_calendar
from datamate.ingest.business import INGEST_VERSION, roles_path, status_path
from datamate.sql.dims import entity_dims
from datamate.text import fold

STORE_VERSION = 7

BASE_MACROS = [
    "create macro round_half_up(x, n) as cast(round(cast(x as decimal(38, 10)), n) as double)",
    "create macro as_of() as getvariable('as_of')",
    "create macro fold_text(x) as replace(strip_accents(lower(cast(x as varchar))), 'đ', 'd')",
    "create macro has_name(col, text) as contains(fold_text(col), fold_text(text))",
    "create macro same_text(col, text) as fold_text(col) = fold_text(text)",
    "create macro pct_change(new, old) as (new - old) * 100.0 / nullif(old, 0)",
    """create macro shift_of(ts) as case when hour(ts) between 6 and 13 then 'sáng'
       when hour(ts) between 14 and 21 then 'chiều' end""",
]
SALES_VIEW = "create view sales_lines as select * from sales_all where \"date\" <= getvariable('as_of')"
ORDERS_VIEW = "create view orders as select * from orders_all where \"date\" <= getvariable('as_of')"
SALES_MACROS = [
    # NULL when the period lies outside the data: no data is not 0 (same condition as dm.require_coverage)
    """create macro revenue(a, b) as (select case when a > as_of() or b < min("date") or a > max("date") then null
       else coalesce(sum(amount) filter (counted and "date" between a and least(b, as_of())), 0) end from sales_all)""",
]
PRICE_MACRO = """create macro price_on(p, d) as (select price from (select {price} as price from sales_all
       where product_name = p and "date" = d) where price is not null group by price order by count(*) desc, price limit 1)"""
ORDER_MACROS = [
    """create macro order_count(a, b) as (select count(*) from orders where counted and "date" between a and b)""",
    """create macro cancel_count(a, b) as (select count(*) filter (status = 'huỷ') from orders
       where "date" between a and b)""",
    """create macro cancel_rate(a, b) as (select round_half_up(
       cast(count(*) filter (status = 'huỷ') as double) / nullif(count(*), 0) * 100, 2) from orders
       where "date" between a and b)""",
    """create macro avg_order_value(a, b) as (select round_half_up(sum(amount) / nullif(count(distinct order_id), 0), 0)
       from sales_lines where counted and "date" between a and b)""",
]


# Rulebook definitions that take several steps: each one exists only where the shop has the fact (facts.py).
DEBT_MACROS = [
    """create view payments as select * from payments_all where "date" <= getvariable('as_of')""",
    """create macro debt_by_order(x) as table select o.order_id, o."date", o.customer, o.customer_name, o.walkin,
       o.total - coalesce(p.paid, 0) as debt, date_diff('day', o."date", least(x, as_of())) as age
       from debt_orders_all o left join (select order_id, sum(amount) as paid from payments_all
         where "date" <= least(x, as_of()) group by order_id) p using (order_id)
       where o.can_owe and o."date" <= least(x, as_of()) and o.total - coalesce(p.paid, 0) > 0.5""",
    """create macro debt_total(x) as (select coalesce(sum(debt), 0) from debt_by_order(x))""",
    """create macro debt_by_customer(x) as table select customer, customer_name, sum(debt) as debt from debt_by_order(x)
       where not walkin group by customer, customer_name order by debt desc, customer""",
    """create macro debt_repaid(a, b) as (select coalesce(sum(p.amount), 0) from payments_all p join debt_orders_all o using (order_id)
       where o.on_credit and p."date" between a and least(b, as_of()) and p."date" > o."date")""",
    """create macro debt_repaid_of(name, a, b) as (select coalesce(sum(p.amount), 0) from payments_all p join debt_orders_all o using (order_id)
       where o.on_credit and o.customer_name = name and p."date" between a and least(b, as_of()) and p."date" > o."date")""",
    """create macro debt_new(a, b) as (select coalesce(sum(greatest(o.total - coalesce(s.paid, 0), 0)), 0) from debt_orders_all o
       left join (select p.order_id, sum(p.amount) as paid from payments_all p join debt_orders_all d using (order_id)
         where p."date" <= d."date" group by p.order_id) s using (order_id)
       where o.on_credit and o."date" between a and least(b, as_of()))""",
    """create macro debt_new_of(name, a, b) as (select coalesce(sum(greatest(o.total - coalesce(s.paid, 0), 0)), 0) from debt_orders_all o
       left join (select p.order_id, sum(p.amount) as paid from payments_all p join debt_orders_all d using (order_id)
         where p."date" <= d."date" group by p.order_id) s using (order_id)
       where o.on_credit and o.customer_name = name and o."date" between a and least(b, as_of()))""",
]
TRIP_MACROS = [
    """create view deliveries as select * from deliveries_all where "date" <= getvariable('as_of')""",
    """create macro delivery_trips(a, b) as (select count(*) from (select distinct order_id, trip from deliveries_all
       where "date" between a and least(b, as_of())))""",
]
IMPORT_MACROS = [
    """create view imports as select * from imports_all where "date" <= getvariable('as_of')""",
    """create macro avg_import_price(p, a, b) as (select round_half_up(sum(amount) / nullif(sum(qty), 0), 0) from imports
       where product_name = p and "date" between a and b)""",
]
SALES_FACT_MACROS = [
    """create macro slow_products(a, b) as table select distinct product_name from sales_lines s
       where counted and is_goods and "date" < a and product_name not in (select product_name from sales_lines
         where counted and is_goods and "date" between a and b and product_name is not null) order by product_name""",
    """create macro price_effect(p, d, n) as (select pct_change(
       sum(qty_base) filter ("date" >= d and "date" < d + cast(n as integer)),
       sum(qty_base) filter ("date" >= d - cast(n as integer) and "date" < d)) from sales_lines where counted and product_name = p)""",
]
STOCK_CARD = """create macro stock_at(p, d) as (select balance from stock_card_all
       where product_name = p and cast(ts as date) <= least(d, as_of()) order by ts desc, seq desc limit 1)"""
STOCK_LOTS = """create macro stock_at(p, d) as (select sum("left") from lots_left(d) where product_name = p)"""
STOCK_REPORT = """create macro stock_at(p, d) as ((select sum(qty) from stock_report_all where product_name = p)
       + coalesce((select sum(qty_base) from sales_all where order_ok and is_goods and product_name = p and "date" > least(d, as_of())), 0)
       - {imports})"""
LOT_MACRO = """create macro lots_left(d) as table select l.lot, l.product_name, l.expiry, l.qty - coalesce(m.moved, 0) as "left", l.price
       from lots_all l left join (select s.lot, sum(s.qty - s.returned) as moved from lot_sales_all s join orders_all o using (order_id)
         where o."date" <= least(d, as_of()) and o.status <> 'huỷ' group by s.lot) m using (lot)
       where l.qty - coalesce(m.moved, 0) > 0"""


class SqlRejected(ValueError):
    """The query is not one SELECT; the model should rewrite it."""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", fold(name)).strip("_")


def _writable(df: pd.DataFrame) -> pd.DataFrame:
    """DuckDB column names are case-insensitive: keep the first of 'Date' and 'date'."""
    seen = pd.Series([str(c).lower() for c in df.columns])
    return df.loc[:, ~seen.duplicated().values]


def _write(con, name: str, df: pd.DataFrame, date_col: str | None = None) -> None:
    con.register("frame", _writable(df))
    cast = f' replace (cast("{date_col}" as date) as "{date_col}")' if date_col else ""
    con.execute(f'create table "{name}" as select *{cast} from frame')
    con.unregister("frame")


def _unit_price(sales: pd.DataFrame) -> str:
    """Price per base unit before line discount, as dm.price_on computes it."""
    if "unit_price" not in sales.columns:
        return "round(amount / qty_base, 2)"
    return "round(unit_price * qty / qty_base, 2)" if "qty" in sales.columns else "round(unit_price, 2)"


def _write_facts(con, f: Facts, sales: pd.DataFrame) -> None:
    """The fact tables and the macros their rules need; a shop without a fact has neither, so the prompt never lists them."""
    for name, df, date_col in (("debt_orders_all", f.orders, "date"), ("payments_all", f.payments, "date"), ("deliveries_all", f.deliveries, "date"),
                               ("imports_all", f.imports, "date"), ("expenses_all", f.expenses, "date"), ("stock_card_all", f.stock_card, None),
                               ("stock_report_all", f.stock_report, None), ("lots_all", f.lots, "expiry"), ("lot_sales_all", f.lot_sales, None),
                               ("product_units", f.units, None)):
        if df is not None:
            _write(con, name, df, date_col)
    if f.orders is not None and f.payments is not None:
        for macro in DEBT_MACROS:
            con.execute(macro)
    if f.deliveries is not None:
        for macro in TRIP_MACROS:
            con.execute(macro)
    if f.imports is not None:
        for macro in IMPORT_MACROS:
            con.execute(macro)
    if "product_name" in sales.columns:
        for macro in SALES_FACT_MACROS:
            con.execute(macro)
    if f.cost_col:
        cost = f'try_cast("{f.cost_col}" as double) * qty'
        con.execute(f"""create macro gross_profit(a, b) as (select sum(amount) - sum({cost}) from sales_lines
            where counted and "date" between a and b)""")
        if f.expenses is not None:
            con.execute("""create macro net_profit(a, b) as (gross_profit(a, b) - (select coalesce(sum(amount), 0) from expenses_all
                where "date" between a and least(b, as_of())))""")
    if f.lots is not None and f.lot_sales is not None:
        con.execute(LOT_MACRO)
    if f.stock_card is not None:
        con.execute(STOCK_CARD)
    elif f.stock_report is not None:
        came = """coalesce((select sum(qty_base) from imports_all where product_name = p and "date" > least(d, as_of())), 0)""" if f.imports is not None else "0"
        con.execute(STOCK_REPORT.format(imports=came))
    elif f.lots is not None and f.lot_sales is not None:
        con.execute(STOCK_LOTS)


def build_store(path: Path, tables: dict[str, pd.DataFrame], roles: dict[str, dict[str, str]],
                calendar: pd.DataFrame | None = None, facts: Facts | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    for macro in BASE_MACROS:
        con.execute(macro)
    if calendar is not None:
        _write(con, "calendar", calendar, "date")
    for name, df in tables.items():
        _write(con, f"raw_{_slug(name)}", df)
    sales, _ = build_sales(tables, roles)
    if sales is not None:
        _write(con, "sales_all", sales, "date")
        con.execute(SALES_VIEW)
        for name, dim in entity_dims(sales).items():
            _write(con, name, dim)
        for macro in SALES_MACROS:
            con.execute(macro)
        if "qty_base" in sales.columns:
            con.execute(PRICE_MACRO.format(price=_unit_price(sales)))
        if "order_id" in sales.columns:
            _write(con, "orders_all", build_orders(sales), "date")
            con.execute(ORDERS_VIEW)
            for macro in ORDER_MACROS:
                con.execute(macro)
        if facts is not None:
            _write_facts(con, facts, sales)
    con.close()
    tmp.replace(path)


def _labels_tag(cache_dir: Path, split: str, biz_id: str) -> str:
    """Saved model answers change the tables, so they name the store too."""
    ingest = Path(cache_dir) / "ingest"
    saved = [p.read_bytes() for p in (status_path(ingest, split, biz_id), roles_path(ingest, split, biz_id)) if p.exists()]
    return f"_{hashlib.sha1(b''.join(saved)).hexdigest()[:8]}" if saved else ""


def store_path(cache_dir: Path, split: str, biz_id: str) -> Path:
    tag = _labels_tag(cache_dir, split, biz_id)
    return Path(cache_dir) / "duckdb" / split / f"{biz_id}_i{INGEST_VERSION}_s{STORE_VERSION}{tag}.duckdb"


def ensure_store(split_dir: Path, biz_id: str, cache_dir: Path, rebuild: bool = False) -> Path:
    path = store_path(cache_dir, split_dir.name, biz_id)
    if rebuild or not path.exists():
        biz = load_business(split_dir, biz_id, Path(cache_dir) / "ingest")
        roles = {n: ti.roles for n, ti in biz.info.items()}
        build_store(path, biz.tables, roles, load_calendar(split_dir), build_facts(biz, build_sales(biz.tables, roles)[0]))
    return path


def set_as_of(con, as_of) -> None:
    con.execute(f"set variable as_of = date '{pd.Timestamp(as_of).date().isoformat()}'")


def connect(path: Path, as_of) -> duckdb.DuckDBPyConnection:
    """Read-only session cut at as_of. One thread keeps float sums and row order the same between runs."""
    con = duckdb.connect(str(path), read_only=True, config={"threads": 1})
    set_as_of(con, as_of)
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")
    return con


_WALL_CLOCK = re.compile(r"\b(current_date|current_timestamp|now\s*\(|today\s*\(|get_current_timestamp)", re.I)


def run_select(con, sql: str, timeout: float = 10.0) -> tuple[list[str], list[tuple]]:
    """(columns, rows) of one SELECT. Raises SqlRejected for anything else, duckdb.Error when it fails or times out."""
    if _WALL_CLOCK.search(sql):
        raise SqlRejected("today is as_of() in this database: do not use current_date, now() or today()")
    statements = con.extract_statements(sql)
    if len(statements) != 1 or statements[0].type.name != "SELECT":
        raise SqlRejected(f"expected exactly one SELECT, got {[s.type.name for s in statements]}")
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        timer.cancel()
