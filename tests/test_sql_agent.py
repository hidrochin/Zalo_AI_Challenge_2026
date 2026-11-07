import datetime as dt
from decimal import Decimal

import duckdb
import pandas as pd
import pytest

from datamate import dm
from datamate.agent.sandbox import name_index
from datamate.canon import build_sales
from datamate.ingest.business import Business, TableInfo, status_path
from datamate.ingest.readme import Readme
from datamate.sql.agent import AnswerShape, MACROS, SqlPool, execute, extract_sql, sql_card, sql_hints, sql_shadow, to_answer
from datamate.sql.store import build_store, connect, run_select, SqlRejected, store_path


def shop():
    lines = pd.DataFrame({
        "ma": ["A", "A", "B", "C"],
        "ngay": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-02"]),
        "gio": pd.to_datetime(["2025-01-01 09:00", "2025-01-01 09:00", "2025-01-02 15:00", "2025-01-02 23:00"]),
        "tien": [100, 50, 70, 30],
        "gia": [100.0, 50.0, 70.0, 15.0],
        "sl": [1, 1, 1, 2],
        "ten": ["Đường", "Muối", "Đường", "Muối"],
        "kh": ["Đỗ Hà", "Đỗ Hà", "Lan", "Lan"],
    })
    roles = {"lines": {"ma": "order_id", "ngay": "order_date", "gio": "order_ts", "tien": "line_amount", "gia": "unit_price",
                       "sl": "qty", "ten": "product_name", "kh": "customer_name"}}
    return {"lines": lines}, roles


@pytest.fixture
def path(tmp_path):
    tables, roles = shop()
    calendar = pd.DataFrame({"date": pd.to_datetime(["2025-01-01"]), "weekday": [3], "lunar_day": [2], "holiday": ["Tết"]})
    p = tmp_path / "shop.duckdb"
    build_store(p, tables, roles, calendar)
    return p


@pytest.fixture
def con(path):
    c = connect(path, "2025-01-31")
    yield c
    c.close()


def one(con, sql):
    return run_select(con, sql)[1][0][0]


def test_scalars_round_like_the_answer_functions():
    assert to_answer("money", ["x"], [(Decimal("1234.5"),)]) == {"type": "number", "answer": 1235}
    assert to_answer("count", ["x"], [(3,)]) == {"type": "number", "answer": 3}
    assert to_answer("number", ["x"], [(1.005,)]) == {"type": "number", "answer": 1.01}
    assert to_answer("percent", ["x"], [(12.345,)]) == {"type": "percent", "answer": 12.35}
    assert to_answer("boolean", ["x"], [(True,)]) == {"type": "boolean", "answer": True}


def test_dates_and_months():
    assert to_answer("date", ["d"], [(dt.date(2025, 3, 4),)]) == {"type": "date", "answer": "2025-03-04"}
    assert to_answer("date", ["d"], [(dt.datetime(2025, 3, 4, 10, 30),)]) == {"type": "date", "answer": "2025-03-04"}
    assert to_answer("month", ["d"], [("2025-03",)]) == {"type": "date", "answer": "2025-03"}
    assert to_answer("month", ["d"], [(dt.date(2025, 3, 1),)]) == {"type": "date", "answer": "2025-03"}


def test_a_name_may_come_with_its_ranking_value():
    assert to_answer("string", ["name", "total"], [("Gạo", 100)]) == {"type": "string", "answer": "Gạo"}


def test_lists_read_a_column_or_a_row():
    assert to_answer("list", ["n"], [("A",), ("B",)]) == {"type": "list", "answer": ["A", "B"]}
    assert to_answer("list", ["a", "b"], [(1, Decimal("2.5"))]) == {"type": "list", "answer": [1, 2.5]}


def test_tables_keep_columns_and_rows():
    assert to_answer("table", ["Khách", "Nợ"], [("An", 100), ("Bình", 50)]) == {
        "type": "table", "answer": {"columns": ["Khách", "Nợ"], "rows": [["An", 100], ["Bình", 50]]}}


@pytest.mark.parametrize("kind,columns,rows", [
    ("number", ["x"], []),
    ("money", ["x"], [(None,)]),
    ("number", ["x"], [(1,), (2,)]),
    ("money", ["a", "b"], [(1, 2)]),
    ("list", ["a", "b"], [(1, 2), (3, 4)]),
    ("boolean", ["x"], [("maybe",)]),
    ("colour", ["x"], [(1,)]),
])
def test_results_that_do_not_fit_the_type_ask_for_a_repair(kind, columns, rows):
    with pytest.raises(AnswerShape):
        to_answer(kind, columns, rows)


def test_extract_takes_the_block_with_a_type_line():
    assert extract_sql("plan\n```sql\n-- type: count\nselect 1\n```\n") == "-- type: count\nselect 1"
    assert extract_sql("```sql\n-- type: count\nselect 1\n```\n```sql\nselect 2\n```") == "-- type: count\nselect 1"
    assert extract_sql("<think>```sql\nselect 9\n```</think>```sql\n-- type: count\nselect 1\n```") == "-- type: count\nselect 1"


def test_extract_unanswerable_and_cut_off_replies():
    assert extract_sql("UNANSWERABLE: không có cột giới tính") == "UNANSWERABLE: không có cột giới tính"
    assert extract_sql("**UNANSWERABLE**: no data") == "**UNANSWERABLE**: no data"
    assert extract_sql("```sql\n-- type: count\nselect 1") == "-- type: count\nselect 1"
    assert extract_sql("Mình không biết") is None


def test_extract_a_query_without_its_opening_fence():
    assert extract_sql("-- type: number\nselect price_on('A', date '2026-04-21')```") == "-- type: number\nselect price_on('A', date '2026-04-21')"
    assert extract_sql("-- type: number\nselect 1```\nĐáp án là 1") == "-- type: number\nselect 1"
    reply = "-- type: number\nselect 1\n\n```json\n{\"type\": \"number\"}\n```"
    assert extract_sql(reply) == "-- type: number\nselect 1"


def test_hints_become_dates():
    hints = ['- "tháng 5/2026" -> month(2026, 5) = 2026-05-01..2026-05-31',
             '- "đến nay" -> until() = start of data..2026-06-01  (point-in-time: everything)']
    assert sql_hints(hints) == ['- "tháng 5/2026": date \'2026-05-01\' .. date \'2026-05-31\'',
                                '- "đến nay": up to and including date \'2026-06-01\'  (point-in-time: everything)']


def test_shadow_needs_one_dated_period():
    one_period = ['- "tháng 5/2026" -> month(2026, 5) = 2026-05-01..2026-05-31']
    assert sql_shadow("q", one_period) == ("q", "2026-05-01", "2026-05-31")
    assert sql_shadow("q", one_period * 2) is None
    assert sql_shadow("q", ['- "đến nay" -> until() = start of data..2026-06-01']) is None
    assert sql_shadow("q", []) is None


def test_revenue_is_null_outside_the_data_and_not_zero(con):
    assert one(con, "select revenue(date '2024-01-01', date '2024-01-31')") is None
    assert one(con, "select revenue(date '2025-03-01', date '2025-03-31')") is None
    assert one(con, "select revenue(date '2024-12-01', date '2025-01-01')") == 150  # partial overlap is computed


def test_helpers_match_dm(con, path):
    tables, roles = shop()
    sales, _ = build_sales(tables, roles)
    dm.set_context("2025-01-31", None, sales)
    assert one(con, "select price_on('Muối', date '2025-01-02')") == dm.price_on("Muối", "2025-01-02")
    assert one(con, "select price_on('Đường', date '2025-01-01')") == dm.price_on("Đường", "2025-01-01")
    assert one(con, "select pct_change(120, 100)") == dm.pct_change(120, 100)
    assert [r[0] for r in run_select(con, "select shift_of(order_ts) from sales_lines order by order_ts")[1]] == [
        dm.shift_of(t) for t in sales.sort_values("order_ts", kind="stable")["order_ts"]]


def test_has_name_ignores_case_and_diacritics(con):
    assert one(con, "select count(*) from sales_lines where has_name(product_name, 'duong')") == 2
    assert one(con, "select count(*) from sales_lines where has_name(customer_name, 'DO HA')") == 2


def test_calendar_is_in_the_database(con):
    assert one(con, "select holiday from calendar where \"date\" = date '2025-01-01'") == "Tết"


def test_wall_clock_is_rejected(con):
    for sql in ("select current_date", "select now()", "select today()", "select CURRENT_TIMESTAMP"):
        with pytest.raises(SqlRejected, match="as_of"):
            run_select(con, sql)
    assert one(con, "select as_of()") == dt.date(2025, 1, 31)


def test_execute_answers_and_sets_as_of(con):
    out = execute(con, "-- type: money\nselect revenue(date '2025-01-01', date '2025-12-31')", "2025-01-01", None)
    assert out["result"] == {"type": "number", "answer": 150} and out["error"] is None


def test_execute_returns_errors_for_a_repair(con):
    assert "type" in execute(con, "select 1", "2025-01-31", None)["error"]
    assert "amount" in execute(con, "-- type: money\nselect amout from sales_lines", "2025-01-31", None)["error"]
    assert "one SELECT" in execute(con, "-- type: money\ndrop table sales_all", "2025-01-31", None)["error"]
    assert "NULL" in execute(con, "-- type: money\nselect revenue(date '2030-01-01', date '2030-01-31')", "2025-01-31", None)["error"]


def test_execute_unanswerable(con):
    out = execute(con, "UNANSWERABLE: không có cột giới tính", "2025-01-31", None)
    assert out["result"] == {"type": "unanswerable", "reason": "không có cột giới tính", "_src": "model"}


def test_execute_applies_the_shared_answer_checks(con):
    tables, roles = shop()
    sales, _ = build_sales(tables, roles)
    names = name_index(tables, sales)
    assert execute(con, "-- type: string\nselect product_name from sales_lines order by 1 limit 1", "2025-01-31", names)["error"] is None
    assert "does not appear" in execute(con, "-- type: string\nselect 'Gạo tám'", "2025-01-31", names)["error"]
    assert "0" in execute(con, "-- type: count\nselect 0", "2025-01-31", names)["warning"]


def test_execute_flags_an_answer_against_the_macro(con):
    shadow = ("doanh thu tháng 1", "2025-01-01", "2025-01-31")
    wrong = execute(con, "-- type: money\nselect 1", "2025-01-31", None, shadow)
    assert "revenue(date '2025-01-01', date '2025-01-31')" in wrong["warning"]
    right = execute(con, "-- type: money\nselect 250", "2025-01-31", None, shadow)
    assert right["warning"] is None


def test_cursors_of_a_pool_keep_their_own_as_of(path):
    pool = SqlPool(path, None, n=2)
    a = pool._free.get()
    b = pool._free.get()
    assert execute(a, "-- type: count\nselect sum(1) from sales_lines", "2025-01-01", None)["result"]["answer"] == 2
    assert execute(b, "-- type: count\nselect sum(1) from sales_lines", "2025-01-31", None)["result"]["answer"] == 4
    assert one(a, "select count(*) from sales_lines") == 2
    pool.close()


def test_card_lists_quoted_columns_and_only_this_shops_macros(path):
    tables, roles = shop()
    tables["Nợ khách"] = pd.DataFrame({"Khách hàng": ["An"], "Còn nợ": [5]})
    biz = Business("x", "train", Readme("", None), tables,
                   {n: TableInfo(name=n, sources=[("f.xlsx", "s")], descs={}, roles=roles.get(n, {})) for n in tables}, [], 0.0)
    sales, desc = build_sales(tables, roles)
    pool = SqlPool(path, None)
    card = sql_card(biz, sales, desc, pool.macros())
    pool.close()
    assert "## View sales_lines" in card and "## View orders" in card and "raw_no_khach" in card
    assert '"Còn nợ"' in card and "orders()" not in card
    assert MACROS["revenue"] in card and MACROS["as_of"] in card


def test_card_without_a_sales_view_says_so():
    tables = {"t": pd.DataFrame({"a": [1]})}
    biz = Business("x", "train", Readme("", None), tables, {"t": TableInfo(name="t", sources=[("f", "s")], descs={}, roles={})}, [], 0.0)
    card = sql_card(biz, None, "", ["revenue", "as_of"])
    assert "no `sales_lines`" in card and "## View" not in card


def test_limit_needs_order_by(tmp_path):
    con = duckdb.connect()
    con.execute("set variable as_of = date '2025-01-01'")
    out = execute(con, "-- type: string\nselect 'a' limit 1", "2025-01-01", None)
    assert "ORDER BY" in out["error"]
    assert execute(con, "-- type: string\nselect 'a' as x order by x limit 1", "2025-01-01", None)["result"]["answer"] == "a"


def test_saved_labels_name_the_store(tmp_path):
    before = store_path(tmp_path, "train", "s")
    status_path(tmp_path / "ingest", "train", "s").parent.mkdir(parents=True)
    status_path(tmp_path / "ingest", "train", "s").write_text("{}", encoding="utf-8")
    assert store_path(tmp_path, "train", "s") != before


def test_lint_stops_query_shapes_that_are_always_wrong():
    from datamate.sql.agent import lint

    assert "percent" in lint("select round_half_up(100.0 * cancel_rate(date '2025-01-01', date '2025-01-31'), 2)")
    assert "count(*)" in lint("select count(*) from sales_lines where counted")
    assert "count(*)" in lint("select customer from sales_lines group by customer having count(*) >= 3")
    assert "counted" in lint("select count(*) from orders where \"date\" = date '2025-01-01'")
    assert "avg_import_price" in lint('select avg("Giá nhập") from raw_x')
    assert "same_text" in lint('select sum(amount) from sales_lines where has_name("Hoạt chất", \'x\')')
    assert "walkin" in lint("select count(*) from (select customer_key from orders where counted group by customer_key having count(distinct order_id) >= 4)")
    assert lint("select customer from orders where counted and not walkin group by customer") is None
    assert lint("select count(*) from orders where counted") is None
    assert lint("select count(*) from (select product_key from sales_lines group by product_key having count(distinct order_id) >= 3)") is None


def test_walkin_filter_added_where_the_source_has_it():
    from datamate.sql.agent import add_not_walkin

    q = "select customer_name from orders\nwhere counted or x group by customer, customer_name order by count(*) desc, customer limit 1"
    assert "where (counted or x) and not walkin\ngroup by customer" in add_not_walkin(q)
    assert "sales_lines s where not walkin\ngroup by customer" in add_not_walkin("select customer from sales_lines s group by customer")
    inner = "select count(*) from (select customer from orders where counted group by customer having count(distinct order_id) >= 4)"
    assert "(counted) and not walkin" in add_not_walkin(inner)
    for kept in ("select customer from sales_lines s join orders o using (order_id) where counted group by customer",  # join: ambiguous
                 "select customer_name from raw_x where a group by customer_name",  # no walkin column
                 "select customer from orders where counted and not walkin group by customer"):
        assert add_not_walkin(kept) == kept


def test_walkins_left_out_of_a_customer_ranking(tmp_path):
    tables, roles = shop()
    tables["lines"].loc[3, "kh"] = "Khách lẻ"
    path = tmp_path / "s.duckdb"
    build_store(path, tables, roles, None)
    con = connect(path, "2025-01-31")
    q = "-- type: list\nselect customer_name from orders where counted group by customer, customer_name order by count(*) desc, customer"
    out = execute(con, q, "2025-01-31", None)
    assert out["error"] is None and out["result"]["answer"] == ["Lan", "Đỗ Hà"]
    assert "not walkin" in out["ran"]


def test_entity_key_alias_rebound_to_the_offered_column():
    from datamate.sql.agent import rebind_key

    err = 'Binder Error: Referenced column "customer_key" not found in FROM clause!\nCandidate bindings: "customer", "customer_name"'
    q = "select customer_name from debt_by_customer(date '2025-07-12') order by debt desc, customer_key limit 5"
    assert rebind_key(q, err).endswith("order by debt desc, customer limit 5")
    assert rebind_key(q, err.replace('"customer", ', "")) .endswith("customer_name limit 5")
    assert rebind_key("select qty_base", 'Referenced column "qty_base" not found in FROM clause!\nCandidate bindings: "_file"') is None


def test_limit_alone_on_a_ranked_macro():
    con = duckdb.connect()
    con.execute("set variable as_of = date '2025-01-01'")
    con.execute("create macro debt_by_customer(x) as table select * from (values ('b', 'B', 5), ('a', 'A', 9), ('c', 'C', 5)) t(customer, customer_name, debt)"
                " order by debt desc, customer")
    out = execute(con, "-- type: list\nselect customer_name from debt_by_customer(date '2025-01-01') limit 2", "2025-01-01", None)
    assert out["error"] is None and out["result"]["answer"] == ["A", "B"]
    assert "ORDER BY" in execute(con, "-- type: list\nselect customer_name from debt_by_customer(date '2025-01-01') where debt > 1 limit 2",
                                 "2025-01-01", None)["error"]
