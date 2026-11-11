"""The agent steps around the solver: rulebook compiler (sql/compile.py), shop views (sql/shopviews.py), judge (agent/judge.py).
A fake model returns fixed replies; the stores are small ones built here."""

import duckdb
import pandas as pd

from datamate.agent.judge import doubt, parse_verdict, solver_text
from datamate.sql.compile import Macro, compile_rulebook, parse_macros, sections, try_macro, usable
from datamate.sql.shopviews import card_part, parse_views, shop_views
from datamate.sql.store import build_store


def store(tmp_path):
    lines = pd.DataFrame({"ma": ["A", "A", "B"], "tien": [100, 50, 70], "sl": [1, 1, 2], "ten": ["Gạo", "Muối", "Gạo"],
                          "ngay": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-02"]), "tt": ["xong", "xong", "huỷ"],
                          "tt__norm": ["hoàn thành", "hoàn thành", "huỷ"], "Ghi chú": ["BS 29A-123", None, "BS 30B-456"]})
    roles = {"lines": {"ma": "order_id", "tien": "line_amount", "sl": "qty", "ten": "product_name", "ngay": "order_date",
                       "tt": "status"}}
    path = tmp_path / "s.duckdb"
    build_store(path, {"lines": lines}, roles)
    return path


REVENUE = """```sql
-- doc: doanh thu từ a đến b
-- args: a date, b date
create macro rev(a, b) as (select coalesce(sum(amount), 0) from sales_lines where counted and "date" between a and b)
```"""
TOP = """```sql
-- doc: mặt hàng bán chạy
-- args: a date, b date, k int
create macro top_items(a, b, k) as table select product_name, sum(qty_base) as q from sales_lines
  where counted and "date" between a and b group by product_name order by q desc, product_name limit k
```"""


def test_sections_and_parse():
    rb = "# R\nintro\n## 1. Doanh thu\n| a | b |\n## 2. Thời gian\nx"
    assert [t for t, _ in sections(rb)] == ["1. Doanh thu", "2. Thời gian"]
    ms = parse_macros(REVENUE + "\n" + TOP, "1")
    assert [(m.name, m.args, m.table) for m in ms] == [("rev", ["a", "b"], False), ("top_items", ["a", "b", "k"], True)]
    assert ms[1].types == {"a": "date", "b": "date", "k": "int"} and ms[0].doc == "doanh thu từ a đến b"
    assert "create or replace temp macro rev(a, b) as (select" in ms[0].temp_sql()
    assert parse_macros("NONE") == []
    two = parse_macros("```sql\n" + REVENUE.split("```sql\n")[1].split("```")[0].strip() + ";\n\n"
                       + TOP.split("```sql\n")[1].split("```")[0].strip() + ";\n```")
    assert [(m.name, m.doc, m.sql.endswith(";")) for m in two] == [("rev", "doanh thu từ a đến b", False),
                                                                  ("top_items", "mặt hàng bán chạy", False)]
    assert two[1].types["k"] == "int" and "-- doc" not in two[0].sql


def test_macros_run_and_hidden_ones_are_refused(tmp_path):
    con = duckdb.connect(str(store(tmp_path)), read_only=True)
    ms = parse_macros(REVENUE + TOP)
    ok = usable(con, ms)
    assert [m.name for m in ok] == ["rev", "top_items"]
    con.execute("set variable as_of = date '2025-01-31'")
    assert con.execute("select rev(date '2025-01-01', date '2025-01-31')").fetchone()[0] == 150
    calls_hand = Macro("r2", ["a", "b"], {}, False, "", "create macro r2(a, b) as (select revenue(a, b))")
    assert "does not exist" in try_macro(con, calls_hand, pd.Timestamp("2025-01-02").date(), frozenset({"revenue"}))
    broken = Macro("bad", ["a"], {}, False, "", "create macro bad(a) as (select nope from sales_lines)")
    assert try_macro(con, broken, pd.Timestamp("2025-01-02").date()) is not None


def test_compile_repairs_once_and_caches(tmp_path):
    path = store(tmp_path)
    calls = []

    def fake(system, parts, history):
        calls.append(len(history))
        if history:  # the repair round
            return REVENUE
        return REVENUE.replace("sum(amount)", "sum(nope)")

    rb = "# R\n## 1. Doanh thu\n| Luật | x |"
    got = compile_rulebook(rb, {"b": path}, fake, tmp_path / "c" / "m.json")
    assert [m.name for m in got] == ["rev"] and got[0].shops == ["b"] and calls == [0, 2]
    assert [m.name for m in compile_rulebook(rb, {"b": path}, fake, tmp_path / "c" / "m.json")] == ["rev"] and len(calls) == 2


def test_shop_views(tmp_path):
    path = store(tmp_path)
    reply = """```sql
-- doc: one row per sales line with a plate number
create view v_plates as select regexp_extract("Ghi chú", '\\d+[A-Z]-\\d+') as plate from raw_lines where "Ghi chú" is not null
```
```sql
-- doc: empty
create view v_none as select * from raw_lines where false
```"""
    assert [n for n, _, _ in parse_views(reply)] == ["v_plates", "v_none"]
    con = duckdb.connect(str(path), read_only=True)
    views = shop_views("b", "card", "readme", con, lambda s, p, h: reply, tmp_path / "v")
    assert [(v.name, v.rows, v.columns) for v in views] == [("v_plates", 2, ["plate"])]
    assert "29A-123" in card_part(con, views)


def test_judge_verdict():
    assert parse_verdict('{"answerable": false, "needs": "giới tính", "where": "không có cột"}') == {
        "answerable": False, "needs": "giới tính", "where": "không có cột"}
    assert parse_verdict("thinking... {\"answerable\": true, \"needs\": 1}")["answerable"] is True
    assert parse_verdict("no idea") is None
    assert solver_text({"type": "unanswerable", "reason": "no gender"}, None) == "UNANSWERABLE: no gender"
    hedged = "```sql\n-- type: table\n-- UNANSWERABLE inside is a comment\nselect 1\n```\n\nUNANSWERABLE: no payments table"
    assert doubt(hedged) == "no payments table"
    assert doubt("```sql\n-- UNANSWERABLE: x\nselect 1\n```") is None and doubt("UNANSWERABLE: no gender") is None
    assert "also wrote: UNANSWERABLE: no payments" in solver_text({"type": "table", "answer": 1}, "select 1", "no payments")
