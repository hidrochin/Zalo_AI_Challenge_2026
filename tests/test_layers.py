"""Capabilities (L0/L2), the frame (L4), the round-trip check (L6) and macro invariants (L1/L10)."""

from datetime import date

import duckdb
import pandas as pd

from datamate.agent.frame import build_frame, consistency
from datamate.capabilities import Capabilities, capability_of, card_part, value_problems
from datamate.facts import Facts
from datamate.sql.agent import MACROS
from datamate.sql.compile import Macro
from datamate.sql.invariants import compiled_problem

HINT = ['- "thang 7/2025" -> month(2025, 7) = 2025-07-01..2025-07-31']


def test_capability_of_question_words():
    assert capability_of("khách còn nợ bao nhiêu tiền") == "debt"
    assert capability_of("doanh thu các đơn trả bằng chuyển khoản tháng 9") is None  # not a delivery
    assert capability_of("trừ thuốc kê đơn ra, các mặt hàng còn lại bán được bao nhiêu") is None  # 'lại' is not 'lãi'
    assert capability_of("tiền bán gỏi xoài khô tháng 5") is None  # 'khô' is not 'kho'
    assert capability_of("Lifebuoy còn mấy cục trong kho") == "stock"


def test_value_checks_flag_payments_above_the_total():
    orders = pd.DataFrame({"order_id": [1, 2], "date": ["2025-01-01"] * 2, "total": [100.0, 200.0]})
    ok = Facts(orders=orders, payments=pd.DataFrame({"order_id": [1, 2], "date": ["2025-01-02"] * 2, "amount": [100.0, 50.0]}))
    assert value_problems(ok) == {}
    bad = Facts(orders=orders, payments=pd.DataFrame({"order_id": [1, 2], "date": ["2025-01-02"] * 2, "amount": [900.0, 50.0]}))
    assert "debt" in value_problems(bad)


def test_card_says_absent_and_suspect_apart():
    caps = Capabilities({"debt": "absent", "stock": "suspect"}, {"stock": "raw TonKho.Tồn"}, {"stock": "no fact built"})
    text = card_part(caps)
    assert "UNANSWERABLE" in text.split("## Read with care")[0] and "công nợ" in text
    assert "TonKho" in text.split("## Read with care")[1]


def test_frame_slots_and_residual():
    f = build_frame("doanh thu thang 7/2025 la bao nhieu", HINT, [], [], "number", (None, None), set(MACROS), MACROS)
    assert f.metric == "revenue" and f.periods == [("thang 7/2025", "2025-07-01", "2025-07-31")] and f.residual == []
    g = build_frame("khách giới tính nữ mua bao nhiêu tháng 7/2025", HINT, [], [], "number", (None, None), set(MACROS), MACROS)
    assert "gioi" in g.residual and "nu" in g.residual
    assert "words not explained above: none" in f.block()


def test_consistency_finds_a_missing_period_and_name():
    f = build_frame("doanh thu Sữa tươi tháng 7/2025", HINT, ["Sữa tươi"], [], "number", (None, None), set(MACROS), MACROS)
    assert consistency(f, "select revenue(date '2025-07-01', date '2025-07-31')") == ["name Sữa tươi"]
    assert consistency(f, "select sum(amount) from sales_lines where product_name = 'Sữa tươi' and \"date\" >= date '2025-06-01'") \
        == ["period 2025-07-01..2025-07-31"]
    assert consistency(f, "UNANSWERABLE: x") == []


def test_compiled_invariants():
    con = duckdb.connect()
    con.execute("create table t as select date '2025-01-05' as d, 10.0 as v")
    good = Macro("sold", ["a", "b"], {"a": "date", "b": "date"}, False, "money sold", "")
    con.execute('create macro sold(a, b) as (select sum(v) from t where d between a and b)')
    assert compiled_problem(con, good, date(2025, 1, 31)) is None
    con.execute("create macro leak(a, b) as (select sum(v) from t)")
    leak = Macro("leak", ["a", "b"], {"a": "date", "b": "date"}, False, "money sold", "")
    assert "after the data" in compiled_problem(con, leak, date(2025, 1, 31))
    con.execute("create macro cancel_rate2(a, b) as (select 250.0)")
    rate = Macro("cancel_rate2", ["a", "b"], {"a": "date", "b": "date"}, False, "tỉ lệ huỷ", "")
    assert "outside 0..100" in compiled_problem(con, rate, date(2025, 1, 31)) or "after the data" in compiled_problem(
        con, rate, date(2025, 1, 31))
