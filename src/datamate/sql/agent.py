"""Text-to-SQL engine: prompt, schema card, and one SELECT turned into a typed answer.

The reply is one ```sql block whose first line declares the answer (`-- type: money`), or `UNANSWERABLE: reason`.
The pipeline around it (preprocess, checks, repair, vote, finalize) is the same as for generated pandas programs.
"""

import datetime as dt
import queue
import re
import sys
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd

from datamate import dm
from datamate.agent.prompt import _THINK, question_part
from datamate.agent.sandbox import check_result
from datamate.canon import build_orders
from datamate.eval.semantic import find_check
from datamate.ingest.business import META_COLS, Business
from datamate.ingest.schema_card import _column_line
from datamate.sql.store import SqlRejected, _slug, _writable, connect, read_plan, run_select, set_as_of, set_params
from datamate.sql.values import ValueIndex
from datamate.text import fold

SYSTEM_TEMPLATE = """You are DataMate, an expert data analyst for Vietnamese small businesses. You answer ONE question about ONE shop's data by writing ONE DuckDB SQL SELECT on a read-only database.

## How your query runs
- The schema card lists this shop's views, raw tables and macros. Use only those, with exact column names. Quote every column name with double quotes: "date", "Thành tiền".
- Today ("hôm nay") is as_of(). Never use current_date, now() or today().
- Views `sales_lines` and `orders` stop at today. `raw_*` tables hold everything in the files, also records dated after today, which have not happened yet: add a `<= as_of()` filter on their date column. An order's cancel/return status is taken as recorded, even if dated later.
- `calendar`: date, weekday (1 = Monday .. 7 = Sunday), lunar_day, lunar_month, lunar_year, lunar_leap, holiday.
- One statement only, no temporary tables. Use WITH for steps.

## Output format
Return ONE ```sql block and nothing after it. Its first line is `-- type: <t>`:
- `money` (VND, rounded to đồng), `count`, `number` (2 decimals), `percent` (12.4 means 12.4%), `boolean`: one row, one column.
- `string` (a name), `date` ('YYYY-MM-DD'), `month` ('YYYY-MM', for "tháng nào"): one row, the value in the first column.
- `list`: top-k names or numbers, one column with one row per item in order; or several numbers asked in a given order, one row with one column per number.
- `table`: one row per object, first column = object name, then the measures in the order asked; name the columns with `as`.
If the data cannot support the question, reply with one line instead: `UNANSWERABLE: <reason>`.
Messages may name answer_number(), answer_string(), unanswerable() and the like: they mean the `-- type:` line and `UNANSWERABLE:`.

## Rules
1. The RULEBOOK below fixes the default meaning of every business term. Apply it exactly unless the question says otherwise. If a macro disagrees with the rulebook, the rulebook wins.
2. Read the README notes: they explain how this shop records returns, cancellations, units, debts, etc. In the views `sales_lines` and `orders` the normalized status is the column "status" (hoàn thành / huỷ / trả hàng / đang giao); in `raw_*` tables a status column with role=status has a normalized copy "<col>__norm" with the same values.
3. Names in answers are copied exactly from the data (or the code, if the data has codes).
4. Answer UNANSWERABLE when a needed table/column does not exist, for subjective questions, external knowledge, or a period entirely outside the data range. Missing data is never 0. An empty result usually means a bug in your filter, not a reason for UNANSWERABLE.
5. Rankings: descending by the criterion, ties by code ascending (ORDER BY amount DESC, code). When fewer than k candidates exist, return all of them.
6. Answer type: a percentage -> percent; "ngày nào" -> date; "tháng nào" -> month; yes/no ("có ... không") -> boolean; top-k or several numbers in a given order -> list; per-object rows with amounts ("bảng", "kèm số tiền", "mỗi người ... bao nhiêu") -> table; one name -> string; otherwise money for VND amounts, count for counts, number for other quantities.
- Use `sales_lines` for anything about sales (revenue, quantities sold, customers): it joins line items with order date/status/customer. Its `counted` column applies the default revenue rule (not cancelled, not returned, no returned line; 'đang giao' counts). Do not filter status = 'hoàn thành' only: use `counted`.
- A macro in the schema card is the tested definition of its rulebook term: call it instead of rebuilding the logic from raw tables. Macros marked TABLE go after FROM.
- Use `orders` (one row per order) for order counts and rates, never line rows. Revenue = sum of line amounts, never order-total columns (those include order discount/shipping).
- If a "Time expressions, already resolved" block follows the question, use exactly those dates.
- "normalized" is the question cleaned up (abbreviations, typos, diacritics, dialect). Use it to understand the question; if it conflicts with the original question, the original wins. Names listed under "names in this shop's data" are exact spellings: compare with `=`. For any other name use has_name(col, 'text'), which ignores case and diacritics.

## Query skeletons (follow this style)
```sql
-- type: money
-- doanh thu tháng 7/2025
select revenue(date '2025-07-01', date '2025-07-31')
```
```sql
-- type: money
-- doanh thu của một mặt hàng; for the quantity sold use sum(qty_base) (base units)
select coalesce(sum(amount), 0) from sales_lines
where counted and product_name = 'Sunlight 750g' and "date" between date '2025-07-01' and date '2025-07-31'
```
```sql
-- type: list
-- top 3 khách chi nhiều nhất tháng 12/2025 (khách lẻ excluded)
select customer_name from sales_lines
where counted and not walkin and "date" between date '2025-12-01' and date '2025-12-31'
group by customer, customer_name order by sum(amount) desc, customer limit 3
```
```sql
-- type: count
-- đơn có tiền hàng từ 290.000 trở lên
select count(*) from orders where counted and goods_amount >= 290000 and "date" between date '2025-07-01' and date '2025-07-31'
```
```sql
-- type: percent
-- tháng 5/2026 tăng/giảm bao nhiêu % so với tháng trước: the previous period of the month ASKED, not of today
select round_half_up(pct_change(revenue(date '2026-05-01', date '2026-05-31'), revenue(date '2026-04-01', date '2026-04-30')), 2)
```
```sql
-- type: count
-- số đơn ca chiều: count orders, not sales_lines rows
select count(*) from orders where counted and shift_of(order_ts) = 'chiều' and "date" between date '2026-02-01' and date '2026-02-28'
```
```sql
-- type: table
-- 3 khách còn nợ nhiều nhất đến hết 30/06/2026; khách còn nợ bao nhiêu: debt_total; tuổi nợ, đơn quá N ngày: debt_age_sum
select customer_name as khach_hang, debt as so_tien_no from debt_by_customer(date '2026-06-30') limit 3
```
```sql
-- type: money
-- tiền khách trả nợ tháng 6/2026; ghi nợ thêm: debt_new; one customer: debt_repaid_of('Tên', ...), debt_new_of('Tên', ...)
select debt_repaid(date '2026-06-01', date '2026-06-30')
```
```sql
-- type: percent
-- X lên giá ngày 01/12/2025: so 30 ngày kể từ ngày đó với 30 ngày ngay trước (quantity, not money)
select round_half_up(price_effect('Đậu hũ chiên giòn', date '2025-12-01', 30), 2)
```
```sql
-- type: count
-- mấy mặt hàng ế từ 21/07 đến 27/07/2025
select count(*) from slow_products(date '2025-07-21', date '2025-07-27')
```
```sql
-- type: date
-- lô gần hết hạn nhất của một thuốc: lots with stock left, not yet expired
select min(expiry) from lots_left(as_of()) where product_name = 'Oresol 245 DHG hộp 20 gói' and expiry >= as_of()
```
```sql
-- type: percent
-- tỉ trọng doanh thu nhóm khách / nhóm hàng: the group column, exactly that group ('trà' is not 'trà sữa'); walk-ins stay in the total
select share_of(customer_group, 'sỉ', date '2026-05-01', date '2026-05-31')
```
```sql
-- type: count
-- khách mua từ 3 đơn trở lên tháng 1/2026 (khách lẻ excluded)
select count(*) from (select customer from orders
  where counted and not walkin and "date" between date '2026-01-01' and date '2026-01-31'
  group by customer having count(distinct order_id) >= 3)
```
```sql
-- type: list
-- doanh thu đơn trả bằng COD và đơn trả bằng tiền mặt: the column and the values written in this shop's data ('Values in this shop's data' block)
select revenue_of("Trả bằng", 'cod', date '2025-03-01', date '2025-03-31'), revenue_of("Trả bằng", 'cash', date '2025-03-01', date '2025-03-31')
```
```sql
-- type: date
-- ngày có doanh thu cao nhất tháng 3/2026; doanh thu ca chiều: shift_of(order_ts) = 'chiều'
select "date" from sales_lines where counted and "date" between date '2026-03-01' and date '2026-03-31'
group by "date" order by sum(amount) desc, "date" limit 1
```

## RULEBOOK (of this split)
{rulebook}
"""

MACROS = {
    "revenue": "revenue(a, b): default-rule revenue from a to b; NULL when the period lies outside the data",
    "order_count": "order_count(a, b): orders that are not cancelled or returned",
    "cancel_count": "cancel_count(a, b): cancelled orders",
    "cancel_rate": "cancel_rate(a, b): percent of cancelled orders among all orders of the period, 2 decimals (12.5 means 12.5%: do not multiply by 100)",
    "debt_total": "debt_total(x): 'khách còn nợ tổng cộng' at the end of day x, walk-in orders included; COD and marketplace orders are never debt",
    "debt_by_order": "debt_by_order(x): TABLE of orders still owing at the end of day x: order_id, date, customer, customer_name, walkin, debt, age (days from the order date). Use it for 'tuổi nợ' and 'đơn đặt quá N ngày'",
    "debt_by_customer": "debt_by_customer(x): TABLE customer, customer_name, debt, biggest first, no walk-in row. Use it for 'top khách nợ'",
    "debt_customers": "debt_customers(x): 'bao nhiêu khách còn nợ': the NUMBER of customers still owing at the end of day x (not an amount)",
    "debt_age_sum": "debt_age_sum(x, lo, hi): debt at the end of day x of the orders whose age in days is from lo to hi (hi NULL = no upper bound). Take the buckets from the question: 'tuổi nợ 0–7, 8–15, trên 15' = select debt_age_sum(x, 0, 7), debt_age_sum(x, 8, 15), debt_age_sum(x, 16, null) as a list; 'quá 7 ngày' = debt_age_sum(x, 8, null)",
    "debt_repaid": "debt_repaid(a, b): 'thu nợ, trả nợ' in the period: payments dated in it, after the day of purchase, on any on-credit order, also of earlier periods",
    "debt_repaid_of": "debt_repaid_of(customer_name, a, b): debt_repaid for one customer",
    "debt_new": "debt_new(a, b): 'nợ thêm': what on-credit orders placed in the period left unpaid at the end of the day of purchase",
    "debt_new_of": "debt_new_of(customer_name, a, b): debt_new for one customer",
    "delivery_trips": "delivery_trips(a, b): 'số đợt giao': distinct (order, trip) pairs delivered in the period",
    "import_total": "import_total(a, b): 'tiền nhập hàng': money of the import receipts dated in the period, cancelled receipts dropped",
    "supplier_stats": "supplier_stats(a, b): TABLE supplier, amount (import money), receipts (number of receipts), biggest amount first. Rulebook: nhà cung cấp nhập nhiều (tiền) nhất = amount",
    "out_of_stock": "out_of_stock(d): TABLE product_name of the goods out of stock at the END of day d ('hết hàng cuối ngày': goods bought to resell, stock 0 after the day's last move); count(*) for 'mấy mặt hàng'",
    "avg_import_price": "avg_import_price(product_name, a, b): 'giá nhập bình quân', weighted by quantity; never avg() of prices",
    "import_minus_sales": "import_minus_sales(product_name, a, b): 'nhập trừ bán, chênh nhập và bán' in base units (sold = counted orders)",
    "gross_profit": "gross_profit(a, b): 'lãi gộp': revenue minus quantity × the cost on each sale line",
    "net_profit": "net_profit(a, b): 'lãi ròng': gross_profit minus every expense of the period",
    "slow_products": "slow_products(a, b): TABLE product_name of 'hàng ế': sold before the period, not in it",
    "price_effect": "price_effect(product_name, d, n): 'hiệu ứng tăng giá': percent change of the QUANTITY sold, n days from day d (d included) vs the n days before d. Use it when the question gives the day",
    "price_effect_at": "price_effect_at(product_name, new_price, n): the same when the question gives the new price, not the day ('từ khi X lên giá Y đ'): the day is the first increase to that price in the price list",
    "price_increase_day": "price_increase_day(product_name, new_price): the first day the price rose to new_price (price list), NULL if never",
    "stock_at": "stock_at(product_name, d): stock at the end of day d in base units",
    "stock_split": "stock_split(product_name, big_unit, d): TABLE one row (whole, loose): 'mấy hộp chẵn, mấy viên lẻ' at the end of day d, whole big units and the rest in base units; answer it as a list of 2",
    "stock_value": "stock_value(d): 'giá trị hàng tồn kho theo giá vốn' at the end of day d: stock left of each product × its cost per base unit in the product list",
    "lots_left": "lots_left(d): TABLE of lots with stock at the end of day d: lot, product_name, expiry, left, price",
    "expiring_lots": "expiring_lots(d, n): TABLE of lots with stock whose expiry is from day d to d + n: lot, product_name, expiry, left, capital (= left × the lot's own price, 'tiền vốn'); count rows for 'mấy lô'",
    "avg_order_value": "avg_order_value(a, b): revenue / counted orders, rounded to đồng",
    "avg_order_value_group": "avg_order_value_group(group, a, b): avg_order_value of one customer group ('khách nhóm G trung bình mỗi đơn')",
    "customer_stats": "customer_stats(a, b): TABLE one row per customer (walk-ins excluded): customer, customer_name, orders, revenue. Rulebook: mua nhiều tiền nhất = revenue; mua nhiều lần/đơn nhất = orders. Ties: order by the measure desc, then customer",
    "customers_min_orders": "customers_min_orders(n, a, b): how many customers have at least n counted orders in the period",
    "share_customers_min_orders": "share_customers_min_orders(n, a, b): percent of the customers who bought in the period that have at least n orders ('khách mua từ N đơn trở lên chiếm mấy %')",
    "item_stats": "item_stats(a, b): TABLE one row per item: product_name, product_key, qty (base units), revenue, orders (orders containing it)[, profit]. bán chạy = qty; nhiều tiền = revenue; có mặt trong nhiều đơn = orders; lời nhất = profit",
    "items_in_min_orders": "items_in_min_orders(n, a, b): how many items appear in at least n counted orders",
    "day_stats": "day_stats(a, b): TABLE one row per day with orders: date, revenue. 'ngày nào doanh thu cao nhất' = revenue; 'mấy ngày/bữa bán được từ X' = count(*) where revenue >= X",
    "month_stats": "month_stats(a, b): TABLE one row per month: month ('YYYY-MM'), revenue, orders. 'tháng nào cao nhất' = select month from month_stats(a, b) order by revenue desc, month limit 1",
    "revenue_rising": "revenue_rising(a, b): true when the revenue of every month from a to b is above the month before it ('doanh thu có tăng liên tục / tháng sau luôn cao hơn tháng trước'): a = the first day of the first month, b = the last day of the last month",
    "revenue_of": "revenue_of(col, value, a, b): revenue (default rule) of the lines whose column col equals value: a channel, payment method, product or customer group, staff, a flag. col is a column of sales_lines, quoted: revenue_of(\"Kênh bán\", 'Zalo OA', a, b). For a yes/no column pass flag(col) and true",
    "share_of": "share_of(col, value, a, b): percent of the shop's whole revenue (walk-ins and fee lines in the total) made by the lines whose col equals value ('nhóm X chiếm bao nhiêu %', 'đơn trả bằng tiền mặt bằng bao nhiêu %')",
    "orders_of": "orders_of(col, value, a, b): counted orders with at least one line whose col equals value ('số đơn có mua nhóm X', 'số đơn qua kênh Y')",
    "qty_of": "qty_of(col, value, a, b): quantity sold in base units of the lines whose col equals value",
    "group_stats": "group_stats(col, a, b): TABLE one row per value of col (staff, channel, group...): value, revenue, orders, qty (base units). 'nhân viên bán giỏi nhất' = select value from group_stats(\"<staff column>\", a, b) order by revenue desc, value limit 1",
    "qty_unit": "qty_unit(unit, a, b): 'bán được bao nhiêu <đơn vị>' with no item named: base-unit quantity of the items whose base unit is that unit ('bao nhiêu ly' = qty_unit('ly', a, b))",
    "flag": "flag(col): a yes/no cell as a boolean whatever the shop writes (true, 1, 'x', 'có'): 'thuốc kê đơn' = flag(\"Thuốc kê đơn\")",
    "shift_revenue": "shift_revenue(s, a, b): revenue of shift s ('sáng' or 'chiều') in the period, shift hours as the rulebook gives them",
    "shift_orders": "shift_orders(s, a, b): counted orders of shift s. Ca chiều vs ca sáng: pct_change(<chiều>, <sáng>), the morning shift is the base",
    "size_qty": "size_qty(s, a, b): cups of size s ('M', 'L') sold: items whose name ends with '(s)', base units",
    "price_on": "price_on(product_name, d): 'giá bán X ngày D', price per base unit before line discount",
    "round_half_up": "round_half_up(x, n): rounds half away from zero",
    "pct_change": "pct_change(new, old): percent change, NULL when old is 0",
    "has_name": "has_name(col, 'text'): col contains text, ignoring case and diacritics",
    "same_text": "same_text(col, 'text'): col equals text, ignoring case, diacritics and punctuation; use it for 'hoạt chất X' (a combination drug is another active ingredient) and 'nhóm X' (exactly that group), has_name would count more",
    "fold_text": "fold_text(x): lowercase without diacritics, underscores as spaces",
    "shift_of": "shift_of(ts): 'sáng' or 'chiều' by the hours of the rulebook's 'Ca sáng, ca chiều' rule, else NULL",
    "as_of": "as_of(): today",
}

_TYPE_LINE = re.compile(r"^\s*--\s*type\s*:\s*(\w+)", re.M)
_UNANSWERABLE = re.compile(r"^\W*UNANSWERABLE\W*:?\s*(.*)$", re.M)
_BLOCK = re.compile(r"```(?:sql|duckdb)?\s*\n(.*?)```", re.S | re.I)
_OPEN_BLOCK = re.compile(r"```(?:sql|duckdb)?\s*\n(.*)$", re.S | re.I)
_BARE_QUERY = re.compile(r"^\s*--\s*type\s*:.*?(?=```|\Z)", re.S | re.M)  # the model left out the opening fence
_HINT = re.compile(r'^- "(.*)" -> .*? = (.+?)\.\.(\d{4}-\d{2}-\d{2})(.*)$')
_MONTH_TEXT = re.compile(r"^\d{4}-\d{2}$")
_SCALARS = {"money", "count", "number", "percent", "boolean"}
_ROW_VALUES = {"string", "date", "month"}


class AnswerShape(ValueError):
    """The query ran, but its result cannot be read as the declared answer type."""


# Generic helpers that hold no rulebook definition: what the "primitives" catalog keeps (RunConfig.catalog)
PRIMITIVES = ("as_of", "has_name", "same_text", "fold_text", "round_half_up", "pct_change", "flag", "shift_of", "stock_at",
              "lots_left")
_SKELETON = re.compile(r"```sql\n.*?```\n", re.S)
_MACRO_RULE = ("- A macro in the schema card is the tested definition of its rulebook term: call it instead of rebuilding the "
               "logic from raw tables. Macros marked TABLE go after FROM.")


_PLAN = ("After the type line write three comment lines, then the query:\n"
         "`-- terms: <each business term of the question> -> <the view, column or macro that holds it>`\n"
         "`-- rule: <the RULEBOOK rule you apply, quoted briefly>`\n"
         "`-- needs: <the tables and columns the query reads>`\n"
         "If a term the question needs has nothing that holds it, reply UNANSWERABLE instead.\n")


def _drop_skeletons(text: str, names) -> str:
    return _SKELETON.sub(lambda m: "" if any(re.search(rf"\b{n}\s*\(", m.group(0)) for n in names) else m.group(0), text)


def build_sql_system(rulebook: str, primitives: bool = False, compiled: bool = False, plan: bool = False,
                     drop: frozenset[str] = frozenset()) -> str:
    """primitives: the skeletons that call a rulebook macro are left out, and the model computes every term itself.
    compiled: the same, but the card also lists the macros the model wrote from this rulebook (sql/compile.py).
    plan: the query starts with comment lines naming the terms, the rule and the columns it uses.
    drop: tested macros this rulebook switched off (catalog "delta"): their skeletons are left out."""
    text = _drop_skeletons(SYSTEM_TEMPLATE, drop) if drop else SYSTEM_TEMPLATE
    if plan:
        text = text.replace("If the data cannot support the question, reply", _PLAN + "If the data cannot support the question, reply")
    if primitives:
        text = _drop_skeletons(text, [m for m in MACROS if m not in PRIMITIVES])
        text = text.replace(_MACRO_RULE, (
            "- The schema card lists generic helpers and the macros written from this RULEBOOK: call one when its doc is the "
            "term the question asks, with the question's own arguments. Compute any other business term yourself from the "
            "views and raw tables, as the RULEBOOK defines it. Macros marked TABLE go after FROM." if compiled else
            "- The macros in the schema card are generic helpers only. Compute every business term "
            "(revenue, debt, stock, profit, rankings...) yourself from the views and raw tables, as the RULEBOOK "
            "defines it. Macros marked TABLE go after FROM."))
    return text.format(rulebook=rulebook.strip())


def _columns(title: str, df: pd.DataFrame, roles: dict | None = None, descs: dict | None = None) -> list[str]:
    roles, descs = roles or {}, descs or {}
    out = [f"\n## {title}  rows={len(df)}"]
    for c in _writable(df).columns:
        if c not in META_COLS:
            out.append(_column_line(df, c, roles.get(c), descs.get(c)).replace(f"`{c}`", f'"{c}"', 1))
    return out


# role of a raw column -> the sales_lines column that holds it (sql_card(rich=True): source tables shown by their mapping)
_SALES_OF_ROLE = {"order_id": "order_id", "order_date": "date", "order_ts": "order_ts", "status": "status", "line_amount": "amount",
                  "qty": "qty", "unit": "unit", "unit_price": "unit_price", "product_name": "product_name",
                  "customer_name": "customer_name", "customer_id": "customer", "base_unit": "base_unit"}
_KEY_ROLES = {"order_id": "order_id", "product_name": "product_name", "customer_name": "customer_name", "customer_id": "customer"}
_SOURCE = re.compile(r"T\['([^']+)'\]")
# canonical fact tables (sql/store.py) and what a shop without any of them lacks
FACTS = ((("payments_all",), "payments received for orders, so debts (công nợ, trả nợ, khách nợ)"),
         (("deliveries_all",), "delivery trips (giao hàng, đợt giao)"),
         (("imports_all",), "import receipts (nhập hàng, nhà cung cấp, giá nhập)"),
         (("expenses_all",), "expenses (chi phí, lãi ròng)"),
         (("stock_card_all", "stock_report_all", "lots_all"), "stock (tồn kho)"),
         (("lots_all",), "lots and expiry dates (lô, hạn dùng)"),
         (("catalog_cost_all",), "a cost price per product in the product list (giá vốn)"))


def absent_part(con) -> str:
    """The card's list of the canonical facts this shop's store lacks: a small model misses what is not there."""
    try:
        have = {r[0] for r in con.execute("select table_name from duckdb_tables()").fetchall()}
    except duckdb.Error:
        return ""
    missing = [what for names, what in FACTS if not have & set(names)]
    if not missing:
        return ""
    return ("\n\n## Not in this shop's canonical tables\n" + "\n".join(f"- {m}" for m in missing)
            + "\nA question that needs one of these is UNANSWERABLE, unless a raw table or a view written for this shop "
              "records it.")


def _source_lines(name: str, df: pd.DataFrame, roles: dict, descs: dict, sales: pd.DataFrame) -> list[str]:
    """A raw table that feeds sales_lines: its columns already there by their sales_lines name, the others in full."""
    mapped = {c: _SALES_OF_ROLE[r] for c, r in roles.items() if r in _SALES_OF_ROLE and _SALES_OF_ROLE[r] in sales.columns
              and c in df.columns}
    out = [f"\n## Table raw_{_slug(name)}  rows={len(df)}  (feeds sales_lines; use sales_lines for these columns: "
           + ", ".join(f'"{c}" = {s}' for c, s in mapped.items()) + ")"]
    for c in _writable(df).columns:
        if c not in META_COLS and c not in mapped and not str(c).endswith("__norm"):
            out.append(_column_line(df, c, roles.get(c), descs.get(c)).replace(f"`{c}`", f'"{c}"', 1))
    return out


def sql_card(biz: Business, sales: pd.DataFrame | None, sales_desc: str, macros: list[str], keep: list[str] | None = None,
             views=None, rich: bool = False) -> str:
    """The prompt's description of this shop's database: views first, then raw tables, then macros.
    keep: the raw tables to list, by slug (table-selection experiment); None lists all of them.
    views: the store's macros, whose fact views (payments, deliveries, imports) the card lists; None = macros.
    rich: raw tables that feed sales_lines list only what sales_lines lacks, and a Keys block says how the raw tables
    join the views."""
    views = macros if views is None else views
    sources = set(_SOURCE.findall(sales_desc)) if rich and sales is not None else set()
    keys = []
    out = [f"# Schema card: {biz.id}"]
    if biz.readme.data_end is not None:
        out.append(f"Data recorded until {biz.readme.data_end:%Y-%m-%d} (from README).")
    if sales is None:
        out.append("This shop has no `sales_lines` or `orders` view: compute from the raw tables.")
    else:
        out += _columns("View sales_lines", sales)
        if "order_id" in sales.columns:
            orders = build_orders(sales)
            out += _columns("View orders", orders)
            money = [c for c in orders.columns if _is_order_money(c) and pd.api.types.is_numeric_dtype(orders[c])
                     and not pd.api.types.is_bool_dtype(orders[c])]
            if money:
                out.append("  note: " + ", ".join(f'"{c}"' for c in money) + " belong to the whole order and are repeated on "
                           "each of its lines: sum or average them over `orders`, never over sales_lines.")
        out.append("\n" + sales_desc.replace("sales_lines()", "sales_lines").replace("orders()", "orders"))
    for name, df in biz.tables.items():
        if keep is not None and f"raw_{_slug(name)}" not in keep:
            continue
        ti = biz.info[name]
        if name in sources:
            out += _source_lines(name, df, ti.roles, ti.descs, sales)
        else:
            out += _columns(f"Table raw_{_slug(name)}  source: {', '.join(sorted({f for f, _ in ti.sources}))}", df, ti.roles, ti.descs)
            if rich and sales is not None:
                keys += [f'- raw_{_slug(name)}."{c}" ~ sales_lines.{_KEY_ROLES[r]}' for c, r in ti.roles.items()
                         if r in _KEY_ROLES and _KEY_ROLES[r] in sales.columns and c in df.columns]
        out += [f"  note: {n}" for n in ti.notes]
    if keys:
        out.append("\n## Keys: how the raw tables join the views (spellings can differ: match names with same_text(a, b))\n"
                   + "\n".join(keys))
    if biz.warnings:
        out.append("\nIngestion warnings: " + "; ".join(biz.warnings))
    if "debt_total" in views:
        out.append("\nView payments: order_id, date, amount (one row per payment).")
    if "delivery_trips" in views:
        out.append("\nView deliveries: order_id, trip, date.")
    if "avg_import_price" in views:
        out.append("\nView imports: doc, date, product_name, qty, unit, price, amount, qty_base (receipts that are not cancelled).")
    out.append("\n## Macros\n" + "\n".join(f"- {MACROS[m]}" for m in MACROS if m in macros))
    return "\n".join(out)


def sql_hints(hints: list[str]) -> list[str]:
    """Time hints with dates instead of dm period calls."""
    out = []
    for h in hints:
        m = _HINT.match(h)
        if m:
            phrase, start, end, note = m.groups()
            span = f"up to and including date '{end}'" if start.startswith("start") else f"date '{start}' .. date '{end}'"
            out.append(f'- "{phrase}": {span}{note}')
    return out


def sql_question_part(question: str, as_of: str, examples: list[dict], hints: list[str] | None = None,
                      norm: dict | None = None) -> str:
    out = question_part(question, as_of, examples, None, norm, fence="sql")
    if (norm or {}).get("values"):
        out += ("\n\nValues in this shop's data whose words the question uses (column = exact value). Use one only if the "
                "question means that value; filter with same_text(col, 'value') or the macros' col, value arguments:\n"
                + "\n".join(norm["values"]))
    if (norm or {}).get("subjective"):
        out += "\n\nNote: " + norm["subjective"]
    if (norm or {}).get("rules"):
        out += ("\n\nRULEBOOK lines whose words the question uses (check that they are the rule it asks; the whole "
                "RULEBOOK is in the system message):\n" + "\n".join(norm["rules"]))
    if (norm or {}).get("frame"):
        out += "\n\n" + norm["frame"]
    lines = sql_hints(hints or [])
    if lines:
        out += "\n\nTime expressions, already resolved by the rulebook (use exactly these dates):\n" + "\n".join(lines)
    return out


def hint_periods(hints: list[str]) -> list[tuple[str | None, str]]:
    """(start, end) of every resolved period; start None = from the start of the data."""
    return [(None if m.group(2).startswith("start") else m.group(2), m.group(3)) for h in hints for m in [_HINT.match(h)] if m]


def periods_outside(periods: list[tuple[str | None, str]], span: tuple[str, str] | None, as_of: str) -> bool:
    """Some period the question names lies entirely outside the data: the rulebook's unanswerable, tested as revenue() does."""
    if not span:
        return False
    lo, hi = span
    return any(b < lo or (a is not None and (a > hi or a > as_of)) for a, b in periods)


def sql_shadow(text: str, hints: list[str]) -> tuple[str, str, str] | None:
    """(question, start, end) when the question names exactly one dated period, for the macro cross-check."""
    spans = [m.groups() for h in hints for m in [_HINT.match(h)] if m]
    if len(spans) != 1 or len(hints) != 1 or spans[0][1].startswith("start"):
        return None
    return text, spans[0][1], spans[0][2]


def extract_sql(text: str) -> str | None:
    """The reply's query with its type line, or its UNANSWERABLE line."""
    text = _THINK.sub("", text or "")
    blocks = _BLOCK.findall(text)
    if blocks:
        return next((b for b in reversed(blocks) if _TYPE_LINE.search(b)), blocks[-1]).strip()
    m = _BARE_QUERY.search(text) or _UNANSWERABLE.search(text)
    if m:
        return m.group(0).strip()
    m = _OPEN_BLOCK.search(text)  # unterminated block: hit max_tokens
    return m.group(1).strip() if m else None


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    return v.isoformat() if isinstance(v, dt.date) else v


_CODE_VALUE = re.compile(r"[A-Za-z]{1,6}[-_.]?\d{2,}")


def drop_code_column(columns: list[str], rows: list[list]) -> tuple[list[str], list[list]]:
    """A code column right before the name column of the same rows (debt_by_customer gives customer, customer_name, debt):
    the reference tables name each row once (train: 19/19 tables are name, amount), so the code goes."""
    if len(columns) < 3 or not rows:
        return columns, rows
    first, second = [r[0] for r in rows], [r[1] for r in rows]
    if all(isinstance(v, str) and _CODE_VALUE.fullmatch(v.strip()) for v in first) \
            and all(isinstance(v, str) and not _CODE_VALUE.fullmatch(v.strip()) for v in second):
        return columns[1:], [r[1:] for r in rows]
    return columns, rows


def to_answer(kind: str, columns: list[str], rows: list[tuple]) -> dict:
    """The typed answer read off a query result. Rounding and formats are those of the dm answer functions."""
    rows = [tuple(_plain(v) for v in r) for r in rows]
    if not rows:
        raise AnswerShape("The query returned no rows. Check the filters; if the data really cannot answer, reply UNANSWERABLE.")
    if kind in _SCALARS | _ROW_VALUES:
        if len(rows) != 1 or (kind in _SCALARS and len(columns) != 1):
            raise AnswerShape(f"`-- type: {kind}` needs one row{' and one column' if kind in _SCALARS else ''}, "
                              f"the query returned {len(rows)} rows and {len(columns)} columns.")
        v = rows[0][0]
        if v is None:
            raise AnswerShape("The query returned NULL: no data matches. Check the filters; if the period lies outside "
                              "the data, reply UNANSWERABLE. Missing data is never 0.")
        if kind in ("money", "count", "number"):
            return dm.answer_number(v, {"number": "decimal"}.get(kind, kind))
        if kind == "percent":
            return dm.answer_percent(v)
        if kind == "boolean":
            if v not in (True, False):
                raise AnswerShape(f"`-- type: boolean` needs true or false, got {v!r}.")
            return dm.answer_bool(v)
        if kind == "string":
            return dm.answer_string(v)
        if isinstance(v, str) and _MONTH_TEXT.match(v.strip()):  # 'YYYY-MM' stays a month whatever the type line says
            v, kind = v.strip() + "-01", "month"
        return dm.answer_date(pd.Timestamp(v)) if kind == "date" else dm.answer_month(pd.Timestamp(v))
    if kind == "list":
        if len(columns) == 1:
            return dm.answer_list([r[0] for r in rows])
        if len(rows) == 1:
            return dm.answer_list(list(rows[0]))
        raise AnswerShape(f"`-- type: list` needs one column, or one row: got {len(rows)} rows and {len(columns)} columns.")
    if kind == "table":
        return dm.answer_table(*drop_code_column(columns, [list(r) for r in rows]))
    raise AnswerShape(f"Unknown answer type {kind!r} in the `-- type:` line.")


def _shadow_warning(con, result: dict, shadow: tuple[str, str, str] | None, timeout: float) -> tuple[str | None, dict | None]:
    """The macro of a semantic contract disagrees with the answer: same role as sandbox.shadow_check."""
    if not shadow or result.get("type") not in ("number", "percent"):
        return None, None
    text, start, end = shadow
    check = find_check(text, result["type"])
    if check is None:
        return None, None
    call = f"{check.name}(date '{start}', date '{end}')"
    try:
        value = run_select(con, f"select {call}", timeout)[1][0][0]
    except duckdb.Error:  # this shop has no such macro: no opinion
        return None, None
    if value is None or abs(float(value) - float(result["answer"])) <= 1e-6:
        return None, None
    warning = (f"Your answer disagrees with the rulebook definition of '{check.name}'. "
               f"Use {call} (it applies the default counting rules) unless the question asks for something else.")
    return warning, {"type": result["type"], "answer": float(value)}


# 'bán được bao nhiêu <đơn vị>' with no item, group or other filter: the rulebook's qty_unit, checked after the query runs
_UNIT_ASK = r"\b(bao nhieu|may|bn)\s+({units})\b(?!\s+(nhieu|tram|tr\b))"
_PLAIN_FILTER = re.compile(r'"?\bdate"?\s+between\s+date\s+\'[\d-]+\'\s+and\s+date\s+\'[\d-]+\'|'
                           r'(same_text\(\s*)?"?\b(unit|base_unit)"?\s*(=|,)\s*\'[^\']*\'\s*\)?|\b(counted|is_goods|and)\b|[();]', re.I)


def unit_asked(text: str, units) -> str | None:
    """The base unit the question counts in ('bán được mấy ly'), matched with its diacritics when the question has them."""
    from datamate.sql.values import _tokens

    f = fold(text)
    by_fold = {}
    for u in units:
        if u and len(fold(u)) > 1:
            by_fold.setdefault(re.sub(r"[^a-z0-9]+", " ", fold(u)).strip(), []).append(u)
    alts = "|".join(sorted(map(re.escape, by_fold), key=len, reverse=True))
    m = re.search(_UNIT_ASK.format(units=alts), f) if alts else None
    if m is None:
        return None
    said = _tokens(text)
    idx = len(_tokens(f[:m.start(2)]))
    word = said[idx] if idx < len(said) else ""
    plain = word == fold(word)
    return next((u for u in by_fold[m.group(2)] if plain or _tokens(u) == [word]), None)


def unit_shadow(code: str, text: str, units) -> str | None:
    """The unit to check the answer against: the query reads sales_lines with no filter but the period, the default rule
    and the unit itself, so the question names no item, group or channel."""
    unit = unit_asked(text, units) if units else None
    if unit is None or not re.search(r"\bsum\s*\(\s*\"?qty_base\"?\s*\)\s+from\s+sales_lines\b", code, re.I):
        return None
    where = re.split(r"\bwhere\b", _body(code), 1, flags=re.I)
    rest = _PLAIN_FILTER.sub(" ", where[1]) if len(where) == 2 else ""
    return unit if not re.search(r"[a-z_\"]", rest, re.I) else None


def _unit_warning(con, result: dict, unit: str, span: tuple[str, str], timeout: float) -> tuple[str | None, dict | None]:
    if result.get("type") != "number":
        return None, None
    call = f"qty_unit('{unit}', date '{span[0]}', date '{span[1]}')"
    try:
        value = run_select(con, f"select {call}", timeout)[1][0][0]
    except duckdb.Error:
        return None, None
    if value is None or abs(float(value) - float(result["answer"])) <= 1e-6:
        return None, None
    return (f"Your answer disagrees with the rulebook's 'Bán được bao nhiêu <đơn vị>': the items whose base unit is "
            f"'{unit}', in base units, also when sold in a bigger unit. Use {call} unless the question asks for something else."), \
        {"type": "number", "answer": float(value)}


# Query shapes that are wrong whatever the shop: they never reach the database, and the message says what to do instead.
_SCALAR_MACRO = r"\b(revenue|order_count|cancel_count|cancel_rate|avg_order_value|gross_profit|net_profit|debt_total)\s*\((?:[^()]|\([^()]*\))*\)"
_LINTS = [
    (re.compile(_SCALAR_MACRO + r"\s*(filter|from|where|over)\b", re.I),
     "revenue(), order_count() and the other scalar macros give ONE number for the whole shop and period: they take no "
     "FILTER, FROM, WHERE or OVER. For part of the sales (a shift, a channel, a payment method, a group, an item) sum "
     "amount over sales_lines where counted and \"date\" between a and b and <condition>, or use shift_revenue / "
     "shift_orders / customer_stats / item_stats."),
    (re.compile(r"\b100(\.0*)?\s*\*\s*cancel_rate\s*\(", re.I), "cancel_rate() already returns percent units: do not multiply it by 100."),
    (re.compile(r"\bcount\(\s*\*\s*\)\s+from\s+sales_lines\b", re.I), "sales_lines has one row per product line, so count(*) counts lines. "
                                                                         "Orders: count(distinct order_id) or the `orders` view. Items sold "
                                                                         "('bán mấy lon/chai'): sum(qty_base)."),
    (re.compile(r"\b(having|order\s+by)\s+count\(\s*\*\s*\)", re.I), None),
    (re.compile(r"\bavg\(\s*\"?(gi[aá] nh[aậ]p|dongianhap|đơn giá|don gia)", re.I), "A mean of prices is not 'giá nhập bình quân': use avg_import_price(), weighted by quantity."),
    (re.compile(r"has_name\(\s*\"?(ho[aạ]t ch[aấ]t|thanhphanchinh|th[aà]nh ph[aầ]n)\"?\s*,\s*'[^']+'", re.I),
     "'Hoạt chất X' means exactly X: use same_text(), has_name also counts combination drugs."),
]
_FROM_LINES = re.compile(r"\bfrom\s+sales_lines\b", re.I)
_FROM_ORDERS = re.compile(r"\bcount\(\s*\*\s*\)\s+from\s+orders\b", re.I)
_STATUS_WORDS = re.compile(r"\bcounted\b|\bstatus(__norm)?\b", re.I)
_BY_CUSTOMER = re.compile(r"\bgroup\s+by\s+\"?customer(_key|_name)?\b", re.I)
_NO_WALKIN = re.compile(r"\bwalkin\b|\bcustomer(_key)?\"?\s+is\s+not\s+null\b", re.I)


_FROM_LINES_ONLY = re.compile(r"\bfrom\s+sales_lines\b", re.I)
_FROM_ORDERS_VIEW = re.compile(r"\b(from|join)\s+orders\b", re.I)


def order_money_lint(code: str, order_cols: tuple[str, ...]) -> str | None:
    """A sum or average over sales_lines of an order field (repeated on every line of the order) counts it once per line."""
    if not order_cols or not _FROM_LINES_ONLY.search(code) or _FROM_ORDERS_VIEW.search(code):
        return None
    for c in order_cols:
        if re.search(rf'\b(sum|avg)\s*\(\s*(?:\w+\.)?"?{re.escape(c)}"?\s*\)', code, re.I):
            return (f'"{c}" belongs to the whole order and is repeated on each of its lines: sum or average it over the `orders` '
                    "view (one row per order; `counted` = not cancelled or returned), not over sales_lines.")
    return None


_EQUALS_TEXT = re.compile(r'"([^"]+)"\s*=\s*\'[^\']*\'')
_FREE_TEXT = re.compile(r"^(ghi chu|dien giai|mo ta|noi dung|chi tiet|ghi chu don)$")


def free_text_lint(code: str) -> str | None:
    """`=` on a column written by hand ('Ghi chú' = 'X') misses the cells that say more ('X, ...')."""
    from datamate.ingest.roles import header_key

    for m in _EQUALS_TEXT.finditer(code):
        if _FREE_TEXT.match(header_key(m.group(1))):
            return (f'"{m.group(1)}" is free text written by hand, so a cell can say more than the word you look for: '
                    f'use has_name("{m.group(1)}", \'...\') (contains, ignoring case and diacritics), not =.')
    return None


_RULE_LINTS = (0, 1, 4, 5)  # _LINTS entries that encode a rulebook term or a macro; the rest are about grain


def lint(code: str, walkin: bool = True, order_cols: tuple[str, ...] = (), rules: bool = True) -> str | None:
    """Why this query shape is wrong, or None. walkin: the views have the `walkin` column (the shop records customers).
    order_cols: money columns of the order repeated on its lines. rules: also the lints that encode rulebook terms."""
    why = order_money_lint(code, order_cols) or free_text_lint(code)
    if why:
        return why
    for i, (rx, message) in enumerate(_LINTS):
        if not rules and i in _RULE_LINTS:
            continue
        if rx.search(code):
            if message:
                return message
            if _FROM_LINES.search(code):
                return ("sales_lines has one row per product line, so count(*) in HAVING or ORDER BY counts lines, not orders. "
                        "Use count(distinct order_id), or group the `orders` view.")
    if not rules:
        return None
    if walkin and _BY_CUSTOMER.search(code) and not _NO_WALKIN.search(code):
        return "Walk-in orders share an empty customer and are not a customer: add `not walkin` before grouping by customer."
    if _FROM_ORDERS.search(code) and not _STATUS_WORDS.search(code):
        return "orders holds every status. Add `counted` to count orders that are not cancelled or returned, as the rulebook's Số đơn says."
    return None


def _depths(code: str) -> list[int]:
    """Parenthesis depth at each character, quotes skipped."""
    out, d, quote = [], 0, None
    for ch in code:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        out.append(d)
    return out


_WORD = re.compile(r"\b(select|from|where|join)\b", re.I)
_WALKIN_SOURCE = re.compile(r"from\s+(sales_lines|orders|debt_by_order\s*\([^()]*\))(\s+(as\s+)?\w+)?(\s+where\b|\s*$)", re.I)
_LINES_SOURCE = re.compile(r"from\s+sales_lines(\s+(as\s+)?\w+)?(\s+where\b|\s*$)", re.I)
_BY_PRODUCT = re.compile(r"\bgroup\s+by\s+\"?product(_key|_name|_id)?\b", re.I)
_NAMED_PRODUCT = re.compile(r"\"?\bproduct_name\"?\s+is\s+not\s+null\b", re.I)


def add_not_walkin(code: str, walkin: bool = True) -> str:
    """`not walkin` added to the SELECT that groups by customer, when its one source has the column; else the code unchanged
    and lint() asks the model. Walk-ins are no customer (rulebook), so the rewrite cannot change a right answer.
    walkin=False: the views have no such column (the shop records no customer), so nothing is added."""
    m = _BY_CUSTOMER.search(code)
    if not walkin or m is None or _NO_WALKIN.search(code):
        return code
    return _add_where(code, m, "not walkin", _WALKIN_SOURCE)


def add_named_products(code: str) -> str:
    """`product_name is not null` added to the SELECT that groups sales_lines by product. Lines without a product (a service
    fee kept in revenue) are no item and are never ranked or counted (rulebook), so the rewrite cannot change a right answer."""
    m = _BY_PRODUCT.search(code)
    if m is None or _NAMED_PRODUCT.search(code):
        return code
    return _add_where(code, m, "product_name is not null", _LINES_SOURCE)


def _add_where(code: str, m: re.Match, cond: str, source: re.Pattern) -> str:
    """`cond` added to the WHERE of the SELECT whose GROUP BY `m` matched, when that SELECT reads one source matching `source`."""
    depth = _depths(code)
    d = depth[m.start()]
    words = [w for w in _WORD.finditer(code, 0, m.start()) if depth[w.start()] == d]
    starts = [i for i, w in enumerate(words) if w.group(1).lower() == "select"]
    if not starts:
        return code
    scope = words[starts[-1] + 1:]
    kinds = [w.group(1).lower() for w in scope]
    if "join" in kinds or kinds.count("from") != 1 or kinds.count("where") > 1:
        return code
    frm = scope[kinds.index("from")]
    if "where" in kinds:
        where = scope[kinds.index("where")]
        if not source.match(code[frm.start():where.end()]):
            return code
        old = code[where.end():m.start()].strip()
        return f"{code[:where.end()]} ({old}) and {cond}\n{code[m.start():]}"
    if not source.match(code[frm.start():m.start()].rstrip()):
        return code
    return f"{code[:m.start()].rstrip()} where {cond}\n{code[m.start():]}"


# The model names an entity key (or the normalized status) the way other tables do; the view calls it otherwise. Rebound only to a column DuckDB offers.
_KEY_ALIASES = {f"{e}_{s}": (f"{e}_key", e, f"{e}_id", f"{e}_name") for e in ("customer", "product") for s in ("key", "code", "id")}
_KEY_ALIASES["status__norm"] = ("status",)  # the raw tables' normalized copy, asked of a view
_MISSING_COLUMN = re.compile(r'Referenced column "([^"]+)" not found.*?Candidate bindings: ([^\n]*)', re.S)


def rebind_key(code: str, error: str) -> str | None:
    """The query with a missing entity-key or status column renamed to the one the source has, or None."""
    m = _MISSING_COLUMN.search(error)
    if m is None or m.group(1).lower() not in _KEY_ALIASES:
        return None
    offered = {c.split(".")[-1] for c in re.findall(r'"([^"]+)"', m.group(2))}
    to = next((c for c in _KEY_ALIASES[m.group(1).lower()] if c in offered and c != m.group(1)), None)
    return re.sub(rf'"?\b{re.escape(m.group(1))}\b"?', to, code) if to else None


TABLE_MACROS = ("customer_stats", "item_stats", "day_stats", "month_stats", "debt_by_order", "debt_by_customer",
                "slow_products", "lots_left", "expiring_lots", "stock_split", "group_stats", "supplier_stats", "out_of_stock")
# macros that already return percent units, never to be scaled or divided again
PERCENT_MACROS = ("share_customers_min_orders", "share_of", "cancel_rate")
_ARGS = r"\((?:[^()']|'[^']*'|\((?:[^()']|'[^']*'|\([^()]*\))*\))*\)"  # a call's argument list, two levels of nesting
_TABLE_COLUMN = re.compile(rf"\b({'|'.join(TABLE_MACROS)})\s*({_ARGS})\s*\.\s*(\"[^\"]+\"|\w+\b)(?!\s*[\[(])", re.I)
_TABLE_ALONE = re.compile(rf"^(\s*select\s+)({'|'.join(TABLE_MACROS)})\s*({_ARGS})\s*;?\s*$", re.I | re.S)
_SCALED = [re.compile(rf"\b100(?:\.0*)?\s*\*\s*((?:{'|'.join(PERCENT_MACROS)})\s*{_ARGS})", re.I),
           re.compile(rf"\b((?:{'|'.join(PERCENT_MACROS)})\s*{_ARGS})\s*\*\s*100(?:\.0*)?\b", re.I),
           re.compile(rf"\b((?:{'|'.join(PERCENT_MACROS)})\s*{_ARGS})\s*/\s*\w+\s*{_ARGS}", re.I)]
_PRICE_GIVEN = re.compile(r"\bprice_effect\s*\(\s*('(?:[^']|'')*')\s*,\s*(\d+(?:\.\d+)?)\s*,", re.I)


def rewrite_macros(code: str, macros=()) -> str:
    """Calls of the store's macros the model writes in a shape that cannot run or is never right, written the right way:
    a TABLE macro's column read as `m(...).col`, a TABLE macro selected alone, a percent macro scaled by 100 or divided by a
    count, price_effect given the new price instead of the day. Each rewrite keeps what the call asks for."""
    body = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("--"))
    head = code[:len(code) - len(code.lstrip())] + "\n".join(line for line in code.splitlines() if line.lstrip().startswith("--"))
    new = _TABLE_COLUMN.sub(lambda m: f"(select {m.group(3)} from {m.group(1)}{m.group(2)})", body)
    new = _TABLE_ALONE.sub(lambda m: f"{m.group(1)}* from {m.group(2)}{m.group(3)}", new)
    for rx in _SCALED:
        new = rx.sub(lambda m: m.group(1), new)
    if not macros or "price_effect_at" in macros:
        new = _PRICE_GIVEN.sub(lambda m: f"price_effect_at({m.group(1)}, {m.group(2)},", new)
    return code if new == body else f"{head.strip()}\n{new}".strip()


_LIMIT = re.compile(r"\blimit\b", re.I)
_ORDER_BY = re.compile(r"\border\s+by\b", re.I)
# Table macros that already return rows in rank order (criterion, then key), so `limit k` alone is a top-k
_RANKED_SOURCE = re.compile(r"^\s*select\b(?:(?!\b(?:join|group\s+by|where)\b).)*\bfrom\s+(debt_by_customer|slow_products)\s*\([^()]*\)\s*(limit\b|$)",
                            re.I | re.S)


def _body(code: str) -> str:
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("--"))


_MACRO_ONLY = [re.compile(r"select\s+(round_half_up\(\s*)?price_effect\([^()]*\)(\s*,\s*\d+\s*\))?", re.I),
               re.compile(r"select\s+count\(\s*\*\s*\)\s+from\s+slow_products\([^()]*\)", re.I)]


def macro_only(code: str) -> bool:
    """The query only reads a macro whose 0 is a real answer (no change in quantity, no slow mover): tested on gold answers."""
    body = re.sub(r"'[^']*'", "''", _body(code)).strip().rstrip(";").strip()
    return any(rx.fullmatch(body) for rx in _MACRO_ONLY)


def execute(con, code: str, as_of: str, names: set[str] | None, shadow=None, timeout: float = 30.0,
            periods: list[tuple[str | None, str]] = (), span: tuple[str, str] | None = None, walkin: bool = True,
            order_cols: tuple[str, ...] = (), units: tuple[str, ...] = (), rules_code: bool = True,
            blocked: frozenset[str] = frozenset()) -> dict:
    """Same record as sandbox._execute, for a reply taken by extract_sql.
    periods: the question's resolved periods; span: first and last day of the sales data; walkin: the views have `walkin`.
    rules_code: the rewrites and lints that encode rulebook terms; blocked: stored macros this run does not offer."""
    m = _UNANSWERABLE.match(code)
    preview, written, n_rows, tables, reads = "", code, None, None, ""
    if m:
        result = {"type": "unanswerable", "reason": m.group(1).strip(), "_src": "model"}
    else:
        declared = _TYPE_LINE.search(code)
        if declared is None:
            return {"result": None, "error": "The first line of the query must be `-- type: <t>`.", "stdout": ""}
        try:
            if _LIMIT.search(code) and not _ORDER_BY.search(code) and not _RANKED_SOURCE.match(_body(code)):
                raise AnswerShape("LIMIT without ORDER BY picks rows at random: order by the criterion, then by code.")
            called = next((m.group(1) for m in re.finditer(r"\b(\w+)\s*\(", _body(code)) if m.group(1) in blocked), None)
            if called:  # as DuckDB says it: the macro is not part of this run's database
                raise SqlRejected(f"Catalog Error: Function with name {called} does not exist!")
            if rules_code:
                code = add_named_products(add_not_walkin(rewrite_macros(code), walkin))
            why = lint(code, walkin, order_cols, rules_code)
            if why:
                raise AnswerShape(why)
            set_as_of(con, as_of)
            for tries in range(3):
                try:
                    columns, rows = run_select(con, code, timeout)
                    break
                except duckdb.BinderException as e:
                    again = rebind_key(code, str(e))
                    if again is None or tries == 2:
                        raise
                    code = again
            preview, n_rows = repr(rows[:5]), len(rows)
            tables, reads = read_plan(con, code)
            result = to_answer(declared.group(1).lower(), columns, rows)
        except (duckdb.Error, ValueError, TypeError) as e:  # ValueError covers SqlRejected and AnswerShape
            error = str(e)[:1500]
            hint = schema_hint(con, code, error, blocked)
            return {"result": None, "error": f"{error}\n{hint}" if hint else error, "stdout": preview, **_ran(written, code)}
    error, warning = check_result(result, names, helper_guard=False, outside=periods_outside(list(periods), span, as_of),
                                  zero_ok=macro_only(code))
    contract = None
    if error is None and warning is None and rules_code:
        warning, contract = _shadow_warning(con, result, shadow, timeout)
        unit = unit_shadow(written, shadow[0], units) if shadow and not warning else None
        if unit:
            warning, contract = _unit_warning(con, result, unit, shadow[1:], timeout)
    return {"result": result if error is None else None, "error": error, "warning": warning, "contract": contract, "stdout": preview,
            "rows": n_rows, "tables": tables, "reads": reads, **_ran(written, code)}


_NO_COLUMN = re.compile(r"Binder Error: Referenced column .* not found|Binder Error: Values list .* does not have a column", re.S)
_NO_TABLE = re.compile(r"Catalog Error: Table with name (\w+) does not exist")
_NO_FUNCTION = re.compile(r"Catalog Error: (?:Scalar |Table )?Function with name (\w+) does not exist")
_FROM_NAME = re.compile(r'\b(?:from|join)\s+(?:"([^"]+)"|(\w+))(?!\s*\()', re.I)


def schema_hint(con, code: str, error: str, blocked: frozenset[str] = frozenset()) -> str:
    """What the database has, for the repair of a query that named a column, table or macro it does not have: every column
    of the tables the query reads (DuckDB itself offers only the closest few), or the tables, or the macros."""
    def names(sql: str, *args) -> list[str]:
        try:
            return [r[0] for r in con.execute(sql, list(args)).fetchall()]
        except duckdb.Error:
            return []

    def q(c: str) -> str:
        return c if re.fullmatch(r"[a-z_][a-z0-9_]*", c) else f'"{c}"'

    if _NO_COLUMN.search(error):
        lines = []
        for t in dict.fromkeys(a or b for a, b in _FROM_NAME.findall(_body(code))):
            cols = names("select column_name from duckdb_columns() where table_name = ? order by column_index", t)
            if cols:
                lines.append(f"Columns of {t}: {', '.join(q(c) for c in cols[:120])}")
        return "\n".join(lines)
    if _NO_TABLE.search(error):
        tables = names("select table_name from duckdb_tables() where not internal union select view_name from duckdb_views() "
                       "where not internal order by 1")
        return f"Tables and views of this database: {', '.join(tables)}" if tables else ""
    if _NO_FUNCTION.search(error):
        fns = [f for f in names("select distinct function_name from duckdb_functions() where not internal order by 1") if f not in blocked]
        return f"Macros of this database: {', '.join(fns)}" if fns else ""
    return ""


def _ran(written: str, code: str) -> dict:
    """The query as executed, in the log, when a rewrite changed the model's query."""
    return {"ran": code} if code != written else {}


_ORDER_OWN = {"order_id", "date", "order_ts", "counted", "goods_amount", "n_lines", "qty_base", "walkin"}
_MONEY_HEAD = re.compile(r"\b(tien|tong|tra|no|giam|bot|thu|phi|chiet khau|ck|gia tri|thanh toan|vat|thue)\b")


def _is_order_money(c: str) -> bool:
    from datamate.ingest.roles import header_key

    return c not in _ORDER_OWN and bool(_MONEY_HEAD.search(header_key(c)))


def order_money_cols(con) -> tuple[str, ...]:
    """Numeric columns of the orders view other than its own: order fields copied onto every sales line."""
    rows = con.execute("select column_name, data_type from duckdb_columns() where table_name = 'orders_all'").fetchall()
    num = ("DOUBLE", "BIGINT", "INTEGER", "DECIMAL", "FLOAT", "HUGEINT", "SMALLINT")
    return tuple(c for c, t in rows if _is_order_money(c) and str(t).upper().startswith(num))


class SqlPool:
    """N cursors on one read-only store; run() is thread-safe, each cursor keeps its own as_of."""

    def __init__(self, path: Path, names: set[str] | None, n: int = 2, timeout: int = 30, params: dict | None = None,
                 rules_code: bool = True):
        """params: the rulebook numbers the macros read (rules.rule_params); None keeps the train rulebook's.
        rules_code: the rewrites, lints and contract checks that encode rulebook terms (off in the "primitives" catalog)."""
        self.base = connect(path, "2000-01-01", params)
        self.names, self.timeout = names, timeout
        self.rules_code, self.blocked = rules_code, frozenset()
        self._free: queue.Queue = queue.Queue()
        for _ in range(max(1, n)):
            cur = self.base.cursor()
            set_params(cur, params)  # each cursor has its own variables
            self._free.put(cur)
        cols = {r[0] for r in self.base.execute("select column_name from duckdb_columns() where table_name = 'sales_all'").fetchall()}
        self.walkin = "walkin" in cols
        lo, hi = self.base.execute('select min("date"), max("date") from sales_all').fetchone() if cols else (None, None)
        self.span = (lo.isoformat(), hi.isoformat()) if lo is not None else None
        self.order_cols = order_money_cols(self.base)
        self.values = ValueIndex.from_store(self.base) if cols else ValueIndex({})
        self.units = tuple(r[0] for r in self.base.execute(
            "select distinct base_unit from sales_all where base_unit is not null").fetchall()) if "base_unit" in cols else ()

    def macros(self) -> list[str]:
        return [r[0] for r in self.base.execute("select distinct function_name from duckdb_functions() where not internal").fetchall()]

    def block(self, names) -> None:
        """Stored macros a query may not call: the read-only store cannot drop them, so run() rejects the call."""
        self.blocked = frozenset(names)

    def setup(self, statements: list[str]) -> None:
        """Run temp-object statements on every cursor (each is its own session): oracle views, compiled macros, shop views.
        DuckDB binds a table macro when it is created, so as_of() needs a typed value first (run() sets the question's).
        A statement that still fails is skipped: one lost macro beats a run with no answers."""
        if not statements:
            return
        day = self.span[1] if self.span else "2000-01-01"
        curs = [self._free.get() for _ in range(self._free.qsize())]
        failed: dict[str, str] = {}
        try:
            for cur in curs:
                cur.execute(f"set variable as_of = date '{day}'")
                for s in statements:
                    try:
                        cur.execute(s)
                    except duckdb.Error as e:
                        failed[s] = str(e).splitlines()[0]
        finally:
            for cur in curs:
                self._free.put(cur)
        for s, err in failed.items():
            print(f"setup skipped: {s.splitlines()[0][:120]} ({err[:200]})", file=sys.stderr, flush=True)

    def run(self, code: str, as_of: str, shadow=None, periods: list[tuple[str | None, str]] = ()) -> dict:
        con = self._free.get()
        try:
            return execute(con, code, str(as_of), self.names, shadow, self.timeout, periods, self.span, self.walkin, self.order_cols,
                           self.units, self.rules_code, self.blocked)
        finally:
            self._free.put(con)

    def close(self) -> None:
        self.base.close()


_PANDAS_CALL = re.compile(r"\b(answer_(\w+)|unanswerable|in_period)\((?:[^()]|\([^()]*\))*\)( -> )?")
_SQL_TERM = {"number": "money/count/number", "percent": "percent", "string": "string", "date": "date", "month": "month",
             "bool": "boolean", "list": "list", "table": "table"}


def sql_terms(msg: str) -> str:
    """The shared answer checks name the pandas helpers (answer_month(...), unanswerable(...)): say it the SQL way, since a
    model told to 'answer with answer_month()' calls a function DuckDB does not have."""
    def one(m: re.Match) -> str:
        if m.group(1) == "unanswerable":
            return "an `UNANSWERABLE: <reason>` line"
        if m.group(1) == "in_period":
            return 'a "date" filter'
        return f"`-- type: {_SQL_TERM.get(m.group(2), m.group(2))}`" + (" with the value " if m.group(3) else "")
    return _PANDAS_CALL.sub(one, msg)


_TABLE_FN = re.compile(r'Function "(\w+)" is a table function but it was used as a scalar function')


def sql_repair_message(exec_out: dict) -> str:
    msg = f"Your query failed:\n{sql_terms(str(exec_out.get('error')))}"
    m = _TABLE_FN.search(str(exec_out.get("error")))
    if m:  # the model repeats `m(...).col` after DuckDB's own message: say how a TABLE macro is read
        msg += (f"\n{m.group(1)}(...) is a TABLE: read it with `select <column> from {m.group(1)}(...) where ...`, "
                f"or `(select <column> from {m.group(1)}(...) where <one row>)` inside an expression.")
    if exec_out.get("stdout"):
        msg += f"\n\nfirst rows: {exec_out['stdout'][-500:]}"
    return msg + "\n\nFix it and return the full corrected query in one ```sql block, with its `-- type:` line."


def sql_check_message(warning: str) -> str:
    return (f"Your query ran, but a sanity check flagged the answer:\n{sql_terms(warning)}\n\n"
            "Return the full query again in one ```sql block (corrected, or unchanged if the answer is right).")
