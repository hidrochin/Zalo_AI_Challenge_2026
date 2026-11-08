"""Text-to-SQL engine: prompt, schema card, and one SELECT turned into a typed answer.

The reply is one ```sql block whose first line declares the answer (`-- type: money`), or `UNANSWERABLE: reason`.
The pipeline around it (preprocess, checks, repair, vote, finalize) is the same as for generated pandas programs.
"""

import datetime as dt
import queue
import re
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
from datamate.sql.store import _slug, _writable, connect, run_select, set_as_of

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
-- 3 khách còn nợ nhiều nhất đến hết 30/06/2026; khách còn nợ bao nhiêu: debt_total; tuổi nợ, đơn quá N ngày: debt_by_order
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
-- tỉ trọng doanh thu nhóm khách: customer_group is on sales_lines, walk-in orders stay in the total
select round_half_up(100.0 * sum(amount) filter (same_text(customer_group, 'sỉ')) / sum(amount), 2)
from sales_lines where counted and "date" between date '2026-05-01' and date '2026-05-31'
```
```sql
-- type: count
-- khách mua từ 3 đơn trở lên tháng 1/2026 (khách lẻ excluded)
select count(*) from (select customer from orders
  where counted and not walkin and "date" between date '2026-01-01' and date '2026-01-31'
  group by customer having count(distinct order_id) >= 3)
```
```sql
-- type: percent
-- tỉ trọng doanh thu nhóm: filter sales_lines on the source column listed in the schema card; exactly that group ('trà' is not 'trà sữa')
select round_half_up(100.0 * sum(amount) filter (same_text("Nhóm hàng", 'hải sản')) / sum(amount), 2)
from sales_lines where counted and "date" between date '2025-03-01' and date '2025-03-31'
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
    "debt_aging": "debt_aging(x): TABLE of one row \"0-7\", \"8-15\", \"over 15\": debt by age in days at the end of day x (for 'tuổi nợ 0–7, 8–15, trên 15')",
    "debt_repaid": "debt_repaid(a, b): 'thu nợ, trả nợ' in the period: payments dated in it, after the day of purchase, on any on-credit order, also of earlier periods",
    "debt_repaid_of": "debt_repaid_of(customer_name, a, b): debt_repaid for one customer",
    "debt_new": "debt_new(a, b): 'nợ thêm': what on-credit orders placed in the period left unpaid at the end of the day of purchase",
    "debt_new_of": "debt_new_of(customer_name, a, b): debt_new for one customer",
    "delivery_trips": "delivery_trips(a, b): 'số đợt giao': distinct (order, trip) pairs delivered in the period",
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
    "customer_stats": "customer_stats(a, b): TABLE one row per customer (walk-ins excluded): customer, customer_name, orders, days (days with an order), revenue, avg_order, cancels. Rulebook: mua nhiều tiền nhất = revenue; trung thành nhất = days; sộp nhất = avg_order among orders >= 3; hay đổi ý nhất = cancels; mua nhiều lần/đơn nhất = orders. Ties: order by the measure desc, then customer",
    "customers_min_orders": "customers_min_orders(n, a, b): how many customers have at least n counted orders in the period",
    "share_customers_min_orders": "share_customers_min_orders(n, a, b): percent of the customers who bought in the period that have at least n orders ('khách mua từ N đơn trở lên chiếm mấy %')",
    "item_stats": "item_stats(a, b): TABLE one row per item: product_name, product_key, qty (base units), revenue, orders (orders containing it)[, profit]. bán chạy = qty; nhiều tiền = revenue; ưa chuộng nhất / có mặt trong nhiều đơn = orders; lời nhất = profit",
    "items_in_min_orders": "items_in_min_orders(n, a, b): how many items appear in at least n counted orders",
    "day_stats": "day_stats(a, b): TABLE one row per day with orders: date, revenue, orders. 'ngày bán nhiều tiền nhất' = revenue; 'ngày đắt khách nhất' = orders; 'mấy ngày/bữa bán được từ X' = count(*) where revenue >= X",
    "month_stats": "month_stats(a, b): TABLE one row per month: month ('YYYY-MM'), revenue, orders. 'tháng nào cao nhất' = select month ... order by revenue desc; growth month after month = compare consecutive rows",
    "shift_revenue": "shift_revenue(s, a, b): revenue of shift s ('sáng' or 'chiều') in the period",
    "shift_orders": "shift_orders(s, a, b): counted orders of shift s. Ca chiều vs ca sáng: pct_change(<chiều>, <sáng>), the morning shift is the base",
    "hour_stats": "hour_stats(a, b): TABLE hour, orders, busiest first. 'giờ cao điểm' = the first hour (a number)",
    "size_qty": "size_qty(s, a, b): cups of size s ('M', 'L') sold: items whose name ends with '(s)', base units",
    "model_line": "model_line(product_name): 'dòng máy': the name before its capacity '…GB' (the whole name when it has none)",
    "brand_of": "brand_of(product_name): 'hãng': the first word of the name; iPhone is Apple",
    "extra_lines": "extra_lines('voucher' | 'ship' | 'điểm', a, b): money of the lines that are not goods (voucher discount, shipping fee, points used) as a positive number, by invoice day, orders not cancelled or returned",
    "price_on": "price_on(product_name, d): 'giá bán X ngày D', price per base unit before line discount",
    "round_half_up": "round_half_up(x, n): rounds half away from zero",
    "pct_change": "pct_change(new, old): percent change, NULL when old is 0",
    "has_name": "has_name(col, 'text'): col contains text, ignoring case and diacritics",
    "same_text": "same_text(col, 'text'): col equals text, ignoring case, diacritics and punctuation; use it for 'hoạt chất X' (a combination drug is another active ingredient) and 'nhóm X' (exactly that group), has_name would count more",
    "fold_text": "fold_text(x): lowercase without diacritics, underscores as spaces",
    "shift_of": "shift_of(ts): 'sáng' for 06:00-13:59, 'chiều' for 14:00-21:59, else NULL",
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


def build_sql_system(rulebook: str) -> str:
    return SYSTEM_TEMPLATE.format(rulebook=rulebook.strip())


def _columns(title: str, df: pd.DataFrame, roles: dict | None = None, descs: dict | None = None) -> list[str]:
    roles, descs = roles or {}, descs or {}
    out = [f"\n## {title}  rows={len(df)}"]
    for c in _writable(df).columns:
        if c not in META_COLS:
            out.append(_column_line(df, c, roles.get(c), descs.get(c)).replace(f"`{c}`", f'"{c}"', 1))
    return out


def sql_card(biz: Business, sales: pd.DataFrame | None, sales_desc: str, macros: list[str], keep: list[str] | None = None) -> str:
    """The prompt's description of this shop's database: views first, then raw tables, then macros.
    keep: the raw tables to list, by slug (table-selection experiment); None lists all of them."""
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
        out += _columns(f"Table raw_{_slug(name)}  source: {', '.join(sorted({f for f, _ in ti.sources}))}", df, ti.roles, ti.descs)
        out += [f"  note: {n}" for n in ti.notes]
    if biz.warnings:
        out.append("\nIngestion warnings: " + "; ".join(biz.warnings))
    if "debt_total" in macros:
        out.append("\nView payments: order_id, date, amount (one row per payment).")
    if "delivery_trips" in macros:
        out.append("\nView deliveries: order_id, trip, date.")
    if "avg_import_price" in macros:
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
        return dm.answer_table(columns, [list(r) for r in rows])
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
                                                                         "('bán mấy máy/chiếc/lon'): sum(qty_base)."),
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


def lint(code: str, walkin: bool = True, order_cols: tuple[str, ...] = ()) -> str | None:
    """Why this query shape is wrong, or None. walkin: the views have the `walkin` column (the shop records customers).
    order_cols: money columns of the order repeated on its lines."""
    why = order_money_lint(code, order_cols)
    if why:
        return why
    for rx, message in _LINTS:
        if rx.search(code):
            if message:
                return message
            if _FROM_LINES.search(code):
                return ("sales_lines has one row per product line, so count(*) in HAVING or ORDER BY counts lines, not orders. "
                        "Use count(distinct order_id), or group the `orders` view.")
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
            order_cols: tuple[str, ...] = ()) -> dict:
    """Same record as sandbox._execute, for a reply taken by extract_sql.
    periods: the question's resolved periods; span: first and last day of the sales data; walkin: the views have `walkin`."""
    m = _UNANSWERABLE.match(code)
    preview, written = "", code
    if m:
        result = {"type": "unanswerable", "reason": m.group(1).strip(), "_src": "model"}
    else:
        declared = _TYPE_LINE.search(code)
        if declared is None:
            return {"result": None, "error": "The first line of the query must be `-- type: <t>`.", "stdout": ""}
        try:
            if _LIMIT.search(code) and not _ORDER_BY.search(code) and not _RANKED_SOURCE.match(_body(code)):
                raise AnswerShape("LIMIT without ORDER BY picks rows at random: order by the criterion, then by code.")
            code = add_named_products(add_not_walkin(code, walkin))
            why = lint(code, walkin, order_cols)
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
            preview = repr(rows[:5])
            result = to_answer(declared.group(1).lower(), columns, rows)
        except (duckdb.Error, ValueError, TypeError) as e:  # ValueError covers SqlRejected and AnswerShape
            return {"result": None, "error": str(e)[:1500], "stdout": preview, **_ran(written, code)}
    error, warning = check_result(result, names, helper_guard=False, outside=periods_outside(list(periods), span, as_of),
                                  zero_ok=macro_only(code))
    contract = None
    if error is None and warning is None:
        warning, contract = _shadow_warning(con, result, shadow, timeout)
    return {"result": result if error is None else None, "error": error, "warning": warning, "contract": contract, "stdout": preview,
            **_ran(written, code)}


def _ran(written: str, code: str) -> dict:
    """The query as executed, in the log, when a rewrite changed the model's query."""
    return {"ran": code} if code != written else {}


_ORDER_OWN = {"order_id", "date", "order_ts", "counted", "goods_amount", "n_lines", "qty_base", "walkin"}
_MONEY_HEAD = re.compile(r"\b(tien|tong|tra|no|giam|bot|thu|phi|ship|chiet khau|ck|gia tri|thanh toan|coc|may cu|vat|thue)\b")


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

    def __init__(self, path: Path, names: set[str] | None, n: int = 2, timeout: int = 30):
        self.base = connect(path, "2000-01-01")
        self.names, self.timeout = names, timeout
        self._free: queue.Queue = queue.Queue()
        for _ in range(max(1, n)):
            self._free.put(self.base.cursor())
        cols = {r[0] for r in self.base.execute("select column_name from duckdb_columns() where table_name = 'sales_all'").fetchall()}
        self.walkin = "walkin" in cols
        lo, hi = self.base.execute('select min("date"), max("date") from sales_all').fetchone() if cols else (None, None)
        self.span = (lo.isoformat(), hi.isoformat()) if lo is not None else None
        self.order_cols = order_money_cols(self.base)

    def macros(self) -> list[str]:
        return [r[0] for r in self.base.execute("select distinct function_name from duckdb_functions() where not internal").fetchall()]

    def run(self, code: str, as_of: str, shadow=None, periods: list[tuple[str | None, str]] = ()) -> dict:
        con = self._free.get()
        try:
            return execute(con, code, str(as_of), self.names, shadow, self.timeout, periods, self.span, self.walkin, self.order_cols)
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


def sql_repair_message(exec_out: dict) -> str:
    msg = f"Your query failed:\n{sql_terms(str(exec_out.get('error')))}"
    if exec_out.get("stdout"):
        msg += f"\n\nfirst rows: {exec_out['stdout'][-500:]}"
    return msg + "\n\nFix it and return the full corrected query in one ```sql block, with its `-- type:` line."


def sql_check_message(warning: str) -> str:
    return (f"Your query ran, but a sanity check flagged the answer:\n{sql_terms(warning)}\n\n"
            "Return the full query again in one ```sql block (corrected, or unchanged if the answer is right).")
