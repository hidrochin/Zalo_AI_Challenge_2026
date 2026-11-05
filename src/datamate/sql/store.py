"""One read-only DuckDB database per business, built once and cached.

Tables: `sales_all`, `orders_all`, `dim_product`, `dim_customer` and every cleaned source table as `raw_<name>`.
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
from datamate.ingest import load_business
from datamate.ingest.business import INGEST_VERSION
from datamate.sql.dims import entity_dims
from datamate.text import fold

STORE_VERSION = 2

ROUNDING = "create macro round_half_up(x, n) as cast(round(cast(x as decimal(38, 10)), n) as double)"
SALES_VIEW = "create view sales_lines as select * from sales_all where \"date\" <= getvariable('as_of')"
ORDERS_VIEW = "create view orders as select * from orders_all where \"date\" <= getvariable('as_of')"
SALES_MACROS = [
    """create macro revenue(a, b) as (select coalesce(sum(amount), 0) from sales_lines
       where counted and "date" between a and b)""",
]
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


def build_store(path: Path, tables: dict[str, pd.DataFrame], roles: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(ROUNDING)
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
        build_store(path, biz.tables, {n: ti.roles for n, ti in biz.info.items()})
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


def run_select(con, sql: str, timeout: float = 10.0) -> tuple[list[str], list[tuple]]:
    """(columns, rows) of one SELECT. Raises SqlRejected for anything else, duckdb.Error when it fails or times out."""
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
