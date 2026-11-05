"""One read-only DuckDB database per business, built once and cached.

Tables: `sales_all`, `orders_all`, `dim_product`, `dim_customer`, `calendar` and every cleaned source table as
`raw_<name>`.
Views `sales_lines` and `orders` cut at the session variable `as_of`, so a query cannot see records that have not
happened yet; `counted` and `status` are computed on the full data first, because an order's cancel or return status
holds even when it is dated after as-of. Macros are the semantic layer, and exist only where the shop has the data.
"""

import re
import threading
from pathlib import Path

import duckdb
import pandas as pd

from datamate.canon import build_orders, build_sales
from datamate.ingest import load_business, load_calendar
from datamate.ingest.business import INGEST_VERSION
from datamate.sql.dims import entity_dims
from datamate.text import fold

STORE_VERSION = 3

BASE_MACROS = [
    "create macro round_half_up(x, n) as cast(round(cast(x as decimal(38, 10)), n) as double)",
    "create macro as_of() as getvariable('as_of')",
    "create macro fold_text(x) as replace(strip_accents(lower(cast(x as varchar))), 'đ', 'd')",
    "create macro has_name(col, text) as contains(fold_text(col), fold_text(text))",
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


def build_store(path: Path, tables: dict[str, pd.DataFrame], roles: dict[str, dict[str, str]],
                calendar: pd.DataFrame | None = None) -> None:
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
    con.close()
    tmp.replace(path)


def store_path(cache_dir: Path, split: str, biz_id: str) -> Path:
    return Path(cache_dir) / "duckdb" / split / f"{biz_id}_i{INGEST_VERSION}_s{STORE_VERSION}.duckdb"


def ensure_store(split_dir: Path, biz_id: str, cache_dir: Path, rebuild: bool = False) -> Path:
    path = store_path(cache_dir, split_dir.name, biz_id)
    if rebuild or not path.exists():
        biz = load_business(split_dir, biz_id, Path(cache_dir) / "ingest")
        build_store(path, biz.tables, {n: ti.roles for n, ti in biz.info.items()}, load_calendar(split_dir))
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
