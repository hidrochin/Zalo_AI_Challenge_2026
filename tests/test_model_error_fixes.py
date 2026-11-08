"""Fixes for model errors on train (debug/sql_train_errors_model.txt): stock macros, month runs, list size, rewrite fidelity."""
import duckdb
import pandas as pd

from datamate.agent.hints import time_hints
from datamate.postprocess import check_answer, expect_answer
from datamate.preprocess import rules_normalize
from datamate.preprocess.rewrite import lost_markers
from datamate.sql.store import BASE_MACROS, STOCK_CARD, STOCK_SPLIT, STOCK_VALUE


def _store():
    con = duckdb.connect()
    for m in BASE_MACROS:
        con.execute(m)
    con.execute("set variable as_of = date '2026-09-20'")
    con.register("card", pd.DataFrame({"product_name": ["Gaviscon", "Gaviscon", "Siro"], "seq": [0, 1, 2], "balance": [100.0, 89.0, 0.0],
                                       "ts": pd.to_datetime(["2026-09-01", "2026-09-10", "2026-09-10"])}))
    con.execute("create table stock_card_all as select * from card")
    con.execute("create table product_units as select 'Gaviscon' as product_name, 'hop' as unit, 24.0 as factor")
    con.execute("create table catalog_cost_all as select * from (values ('Gaviscon', 5000.5), ('Siro', 9000.0)) t(product_name, cost)")
    for m in (STOCK_CARD, STOCK_SPLIT, STOCK_VALUE):
        con.execute(m)
    return con


def test_stock_split_and_value():
    con = _store()
    assert con.execute("select * from stock_split('Gaviscon', 'Hộp', date '2026-09-20')").fetchone() == (3, 17)
    assert con.execute("select stock_value(date '2026-09-20')").fetchone()[0] == 445045  # 89 × 5000.5, Siro has no stock


def test_month_run_lists_every_month():
    hints = time_hints(rules_normalize("Doanh thu 3 tháng liền nhau kết thúc ở tháng 11/2025 là bao nhiêu mỗi tháng?").text, "2026-07-09")
    assert [h.split(" -> ")[1].split(" =")[0] for h in hints] == ["month(2025, 9)", "month(2025, 10)", "month(2025, 11)"]


def test_whole_and_loose_is_a_list_of_two():
    e = expect_answer(rules_normalize("ton kho gaviscon hop 24 goi cuoi ngay hom nay dc may hop chan va may goi le").text)
    assert e.type == "list" and e.size == 2
    why, hard = check_answer({"type": "list", "answer": [89]}, e)
    assert why and hard
    assert check_answer({"type": "list", "answer": [3, 17]}, e) == (None, False)


def test_rewrite_keeps_business_words():
    assert lost_markers("Xem giùm doanh thu ca chiều tuần trước", "Xem giúp doanh thu của chiều tuần trước?") == ["ca chieu"]
    assert lost_markers("Số ly size M bán được, cộng mọi món", "Số ly size M bán được của mỗi món") == ["cong", "mọi"]
    assert lost_markers("dthu ca chiều tuần trước", "Doanh thu ca chiều tuần trước là bao nhiêu?") == []
