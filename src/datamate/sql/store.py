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
from datamate.ingest.business import INGEST_VERSION, roles_path, slots_path, status_path
from datamate.sql.dims import entity_dims
from datamate.text import fold

STORE_VERSION = 15

BASE_MACROS = [
    "create macro round_half_up(x, n) as cast(round(cast(x as decimal(38, 10)), n) as double)",
    "create macro as_of() as getvariable('as_of')",
    # lowercase, no diacritics, '_' a space ('zalo_oa' = 'Zalo OA'); punctuation stays, it can be the point of a search ('(M)')
    """create macro fold_text(x) as trim(regexp_replace(strip_accents(replace(lower(cast(x as varchar)), 'đ', 'd')),
       '[_\\s]+', ' ', 'g'))""",
    "create macro has_name(col, text) as contains(fold_text(col), fold_text(text))",
    # equality also ignores punctuation, as the scorer compares names ('Thu hộ (COD)' = 'thu ho cod')
    """create macro same_text(col, text) as trim(regexp_replace(fold_text(col), '[^a-z0-9]+', ' ', 'g'))
       = trim(regexp_replace(fold_text(text), '[^a-z0-9]+', ' ', 'g'))""",
    "create macro pct_change(new, old) as (new - old) * 100.0 / nullif(old, 0)",
    # shift hours are read from the split's rulebook at session start (rules.rule_params): NULL hours, no shift
    """create macro shift_of(ts) as case
       when hour(ts) between getvariable('shift_morning_from') and getvariable('shift_morning_to') then 'sáng'
       when hour(ts) between getvariable('shift_afternoon_from') and getvariable('shift_afternoon_to') then 'chiều' end""",
    # a yes/no cell however the shop writes it: true, 1, 'x', 'có', 'yes'
    """create macro flag(x) as coalesce(try_cast(x as boolean),
       fold_text(x) in ('x', 'co', 'yes', 'y', 'true', '1', 'v'), false)""",
]
SHIFT_DEFAULTS = {"shift_morning_from": 6, "shift_morning_to": 13, "shift_afternoon_from": 14, "shift_afternoon_to": 21}
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


# The rulebook's rankings and counts as tables of one row per customer / item / day / month: the model picks a column
# and a sort instead of rebuilding the grain (orders, base units) each time. Ties go to the smaller key (code).
CUSTOMER_STATS = """create macro customer_stats(a, b) as table select customer, any_value(customer_name) as customer_name,
       count(distinct order_id) filter (counted) as orders, coalesce(sum(amount) filter (counted), 0) as revenue
       from sales_lines where customer is not null and "date" between a and b group by customer order by revenue desc, customer"""
ITEM_STATS = """create macro item_stats(a, b) as table select product_name, min(product_key) as product_key,
       coalesce(sum(qty_base) filter (counted), 0) as qty, coalesce(sum(amount) filter (counted), 0) as revenue,
       count(distinct order_id) filter (counted) as orders{profit}
       from sales_lines where is_goods and product_name is not null and "date" between a and b
       group by product_name order by qty desc, product_key"""
DAY_STATS = """create macro day_stats(a, b) as table select "date", coalesce(sum(amount) filter (counted), 0) as revenue
       from sales_lines where "date" between a and least(b, as_of())
       group by "date" order by 1"""
MONTH_STATS = """create macro month_stats(a, b) as table select strftime("date", '%Y-%m') as month,
       coalesce(sum(amount) filter (counted), 0) as revenue, count(distinct order_id) filter (counted) as orders
       from sales_lines where "date" between a and least(b, as_of()) group by month order by month"""
# 'doanh thu có tăng liên tục qua các tháng': every month of [a, b] above the month before it (months with no sale count 0)
REVENUE_RISING = """create macro revenue_rising(a, b) as (select bool_and(r > p) from (select r, lag(r) over (order by m) as p
       from (select m, coalesce((select sum(amount) from sales_lines where counted and "date" >= m
         and "date" < m + interval 1 month and "date" <= b), 0) as r
         from (select unnest(generate_series(date_trunc('month', a), date_trunc('month', b), interval 1 month)) as m)))
       where p is not null)"""
# Part of the sales picked by the value of one column (channel, payment method, product or customer group, staff, a yes/no
# flag): the rulebook's default revenue and order rules, the column matched exactly (same_text) as 'Nhóm hàng' says.
PART_MACROS = [
    """create macro revenue_of(col, v, a, b) as (select coalesce(sum(amount), 0) from sales_lines
       where counted and same_text(col, v) and "date" between a and b)""",
    # rulebook: a share divides by the revenue of the whole shop in the period, walk-ins and fee lines included
    """create macro share_of(col, v, a, b) as (select round_half_up(100.0 * coalesce(sum(amount) filter (same_text(col, v)), 0)
       / nullif(sum(amount), 0), 2) from sales_lines where counted and "date" between a and b)""",
    """create macro orders_of(col, v, a, b) as (select count(distinct order_id) from sales_lines
       where counted and same_text(col, v) and "date" between a and b)""",
    """create macro qty_of(col, v, a, b) as (select coalesce(sum(qty_base), 0) from sales_lines
       where counted and is_goods and same_text(col, v) and "date" between a and b)""",
    # one row per value of a column (staff, channel, group...): rank or compare them; NULL values are no group
    """create macro group_stats(col, a, b) as table select cast(col as varchar) as "value",
       coalesce(sum(amount) filter (counted), 0) as revenue, count(distinct order_id) filter (counted) as orders,
       coalesce(sum(qty_base) filter (counted and is_goods), 0) as qty
       from sales_lines where col is not null and "date" between a and b group by 1 order by revenue desc, 1""",
]
# rulebook 'Bán được bao nhiêu <đơn vị>': base-unit quantity of the items whose base unit is that unit
QTY_UNIT = """create macro qty_unit(u, a, b) as (select coalesce(sum(qty_base), 0) from sales_lines
       where counted and is_goods and same_text(base_unit, u) and "date" between a and b)"""
SHIFT_MACROS = [
    """create macro shift_revenue(s, a, b) as (select coalesce(sum(amount) filter (counted), 0) from sales_lines
       where same_text(shift_of(order_ts), s) and "date" between a and b)""",
    """create macro shift_orders(s, a, b) as (select count(distinct order_id) filter (counted) from sales_lines
       where same_text(shift_of(order_ts), s) and "date" between a and b)""",
]
COUNT_MACROS = [
    """create macro customers_min_orders(n, a, b) as (select count(*) from customer_stats(a, b) where orders >= n)""",
    """create macro share_customers_min_orders(n, a, b) as (select round_half_up(100.0 * count(*) filter (orders >= n)
       / nullif(count(*) filter (orders >= 1), 0), 2) from customer_stats(a, b))""",
]
ITEM_COUNT_MACRO = """create macro items_in_min_orders(n, a, b) as (select count(*) from item_stats(a, b) where orders >= n)"""
GROUP_AOV = """create macro avg_order_value_group(g, a, b) as (select round_half_up(sum(amount) filter (counted)
       / nullif(count(distinct order_id) filter (counted), 0), 0) from sales_lines
       where same_text(customer_group, g) and "date" between a and b)"""
SIZE_QTY = """create macro size_qty(sz, a, b) as (select coalesce(sum(qty_base), 0) from sales_lines where counted and is_goods
       and regexp_matches(lower(product_name), '\\(' || lower(sz) || '\\)\\s*$') and "date" between a and b)"""
PRICE_LIST_FROM_SALES = """create table price_list_all as select product_name, "date", price from (
       select product_name, "date", {price} as price, count(*) as n from sales_all
       where order_ok and is_goods and product_name is not null and qty_base > 0 group by all)
       where price is not null qualify row_number() over (partition by product_name, "date" order by n desc, price) = 1"""
PRICE_MACROS = [
    # rulebook: an increase is a day whose new price is above the one before; to the same price several times, the first
    """create macro price_increase_day(p_name, new_price) as (select min("date") from (select "date", price,
       lag(price) over (order by "date") as prev from price_list_all where product_name = p_name)
       where price = new_price and price > prev)""",
    """create macro price_effect_at(p_name, new_price, n) as (price_effect(p_name, price_increase_day(p_name, new_price), n))""",
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
    # age buckets come from the question: debt_age_sum(x, 0, 7), (x, 8, 15), (x, 16, null) for '0–7, 8–15, trên 15'
    """create macro debt_age_sum(x, lo, hi) as (select coalesce(sum(debt), 0) from debt_by_order(x)
       where age >= lo and (hi is null or age <= hi))""",
    """create macro debt_by_customer(x) as table select customer, customer_name, sum(debt) as debt from debt_by_order(x)
       where not walkin group by customer, customer_name order by debt desc, customer""",
    """create macro debt_customers(x) as (select count(*) from debt_by_customer(x))""",
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
    # rulebook 'Tiền nhập hàng trong kỳ': receipts dated in the period, cancelled ones dropped (imports_all has none)
    """create macro import_total(a, b) as (select coalesce(sum(amount), 0) from imports where "date" between a and b)""",
    # rulebook 'nhập trừ bán': the sold part is the quantity of orders counted in revenue, in the same (base) unit
    """create macro import_minus_sales(p, a, b) as ((select coalesce(sum(qty_base), 0) from imports
       where product_name = p and "date" between a and b) - (select coalesce(sum(qty_base), 0) from sales_lines
       where counted and product_name = p and "date" between a and b))""",
]
SUPPLIER_STATS = """create macro supplier_stats(a, b) as table select supplier, coalesce(sum(amount), 0) as amount,
       count(distinct doc) as receipts from imports where supplier is not null and "date" between a and b
       group by supplier order by amount desc, supplier"""
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
# rulebook 'Tồn theo đơn vị lớn': [whole big units, loose base units]; 'Giá trị hàng tồn kho': stock left x catalogue cost
STOCK_SPLIT = """create macro stock_split(p, u, d) as table select cast(floor(s / f) as bigint) as whole,
       cast(round(s - f * floor(s / f)) as bigint) as loose from (select stock_at(p, d) as s,
       (select factor from product_units where product_name = p and same_text(unit, u) limit 1) as f)"""
STOCK_VALUE = """create macro stock_value(d) as (select round_half_up(sum(s * cost), 0) from
       (select cost, stock_at(product_name, d) as s from catalog_cost_all) where s > 0)"""
# rulebook 'Mặt hàng hết hàng cuối ngày': goods bought to resell (the ones the stock records track, not drinks made on
# the spot) whose stock at the END of day d is 0; one sold out at noon and restocked before night is not
# the outer name is `item`: stock_at(product_name, d) in a WHERE would bind product_name to stock_at's own table
OUT_OF_STOCK = """create macro out_of_stock(d) as table select item as product_name from (select distinct product_name as item
       from {items}) where item is not null and coalesce(stock_at(item, d), 0) <= 0 order by item"""
# the goods each stock source tracks, by the end of day d
STOCK_ITEMS = {"card": "stock_card_all where cast(ts as date) <= least(d, as_of())", "report": "stock_report_all",
               "lots": "lots_all"}
EXPIRING_LOTS = """create macro expiring_lots(d, n) as table select lot, product_name, expiry, "left", "left" * price as capital
       from lots_left(d) where expiry between d and d + cast(n as integer) order by expiry, product_name"""
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
                               ("product_units", f.units, None), ("catalog_cost_all", f.catalog_cost, None)):
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
        if "supplier" in f.imports.columns and f.imports["supplier"].notna().any():
            con.execute(SUPPLIER_STATS)
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
        con.execute(EXPIRING_LOTS)
    if "product_name" in sales.columns and "qty_base" in sales.columns:
        if f.price_list is not None:
            _write(con, "price_list_all", f.price_list, "date")
        else:
            con.execute(PRICE_LIST_FROM_SALES.format(price=_unit_price(sales)))
        for macro in PRICE_MACROS:
            con.execute(macro)
    source = None
    if f.stock_card is not None:
        con.execute(STOCK_CARD)
        source = "card"
    elif f.stock_report is not None:
        came = """coalesce((select sum(qty_base) from imports_all where product_name = p and "date" > least(d, as_of())), 0)""" if f.imports is not None else "0"
        con.execute(STOCK_REPORT.format(imports=came))
        source = "report"
    elif f.lots is not None and f.lot_sales is not None:
        con.execute(STOCK_LOTS)
        source = "lots"
    has_stock = source is not None
    if has_stock:
        con.execute(OUT_OF_STOCK.format(items=STOCK_ITEMS[source]))
    if has_stock and f.units is not None:
        con.execute(STOCK_SPLIT)
    if has_stock and f.catalog_cost is not None:
        con.execute(STOCK_VALUE)


def build_store(path: Path, tables: dict[str, pd.DataFrame], roles: dict[str, dict[str, str]],
                calendar: pd.DataFrame | None = None, facts: Facts | None = None, slots: dict | None = None) -> None:
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
    sales, _ = build_sales(tables, roles, slots)
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
            con.execute(DAY_STATS)
            con.execute(MONTH_STATS)
            con.execute(REVENUE_RISING)
            if "qty_base" in sales.columns:
                for macro in PART_MACROS:
                    con.execute(macro)
            if "base_unit" in sales.columns and "qty_base" in sales.columns:
                con.execute(QTY_UNIT)
            if "customer" in sales.columns:
                con.execute(CUSTOMER_STATS)
                for macro in COUNT_MACROS:
                    con.execute(macro)
            if "product_name" in sales.columns and "qty_base" in sales.columns:
                cost = facts.cost_col if facts is not None else None
                profit = (f""", sum(amount) filter (counted) - sum(try_cast("{cost}" as double) * qty) filter (counted) as profit"""
                          if cost else "")
                con.execute(ITEM_STATS.format(profit=profit))
                con.execute(ITEM_COUNT_MACRO)
                if sales["product_name"].astype(str).str.contains(r"\((?:S|M|L)\)\s*$", case=False).any():
                    con.execute(SIZE_QTY)  # drink sizes written in the name: 'Trà vải (L)'; not diaper sizes
            if "order_ts" in sales.columns:
                for macro in SHIFT_MACROS:
                    con.execute(macro)
            if "customer_group" in sales.columns:
                con.execute(GROUP_AOV)
        if facts is not None:
            _write_facts(con, facts, sales)
    con.close()
    tmp.replace(path)


def _labels_tag(cache_dir: Path, split: str, biz_id: str) -> str:
    """Saved model answers change the tables, so they name the store too."""
    ingest = Path(cache_dir) / "ingest"
    saved = [p.read_bytes() for p in (status_path(ingest, split, biz_id), roles_path(ingest, split, biz_id),
                                      slots_path(ingest, split, biz_id)) if p.exists()]
    return f"_{hashlib.sha1(b''.join(saved)).hexdigest()[:8]}" if saved else ""


def store_path(cache_dir: Path, split: str, biz_id: str) -> Path:
    tag = _labels_tag(cache_dir, split, biz_id)
    return Path(cache_dir) / "duckdb" / split / f"{biz_id}_i{INGEST_VERSION}_s{STORE_VERSION}{tag}.duckdb"


def ensure_store(split_dir: Path, biz_id: str, cache_dir: Path, rebuild: bool = False) -> Path:
    path = store_path(cache_dir, split_dir.name, biz_id)
    if rebuild or not path.exists():
        biz = load_business(split_dir, biz_id, Path(cache_dir) / "ingest")
        roles = {n: ti.roles for n, ti in biz.info.items()}
        build_store(path, biz.tables, roles, load_calendar(split_dir), build_facts(biz, build_sales(biz.tables, roles, biz.slots)[0]),
                    biz.slots)
    return path


def set_as_of(con, as_of) -> None:
    con.execute(f"set variable as_of = date '{pd.Timestamp(as_of).date().isoformat()}'")


def set_params(con, params: dict | None = None) -> None:
    """Rulebook parameters of the macros (shift hours), per session: each cursor has its own variables.
    None keeps the train rulebook's values; a None value (the rule could not be read) makes the macros return NULL."""
    for k, v in {**SHIFT_DEFAULTS, **(params or {})}.items():
        con.execute(f"set variable {k} = {'null' if v is None else int(v)}")


def connect(path: Path, as_of, params: dict | None = None) -> duckdb.DuckDBPyConnection:
    """Read-only session cut at as_of. One thread keeps float sums and row order the same between runs."""
    con = duckdb.connect(str(path), read_only=True, config={"threads": 1})
    set_as_of(con, as_of)
    set_params(con, params)
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
