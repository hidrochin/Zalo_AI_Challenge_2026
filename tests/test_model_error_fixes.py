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


def test_frame_advice_train_errors_of_the_route_run():
    from datamate.agent.frame import Frame
    from datamate.agent.lint import frame_advice

    f = Frame()
    shift = "-- type: percent\nselect pct_change(shift_revenue('chiều', date '2025-12-01', date '2025-12-31'), shift_revenue('sáng', date '2025-12-01', date '2025-12-31'))"
    assert "shift_orders" in frame_advice("tháng 12/2025, số đơn ca chiều tăng hay giảm bao nhiêu % so với ca sáng", shift, f)
    assert frame_advice("tháng 12/2025, doanh thu ca chiều tăng hay giảm bao nhiêu % so với ca sáng", shift, f) is None
    one = "-- type: percent\nselect shift_orders('chiều', date '2025-10-06', date '2025-10-12')"
    assert "pct_change" in frame_advice("tuần rồi, số đơn ca chiều tăng hay giảm bao nhiêu % so với ca sáng", one, f)
    lines = "-- type: string\nselect customer_name from sales_lines group by 1 order by count(*) desc, 1 limit 1"
    assert "distinct order_id" in frame_advice("khách nào mua nhiều đơn nhất", lines, f)
    stock = Frame(entities=["Povidine 10% chai 90ml"], capability=("stock", "trusted"))
    raw = "-- type: number\nselect \"Tồn cuối kỳ\" from raw_baocaotonkho where \"Mã hàng\" = 'SP0001'"
    assert "SP0001" in frame_advice("tồn kho povidine 10% chai 90ml cuối ngày hôm nay", raw, stock)
    by_name = "-- type: number\nselect \"Tồn cuối kỳ\" from raw_baocaotonkho where \"Tên hàng\" = 'Povidine 10% chai 90ml'"
    assert frame_advice("tồn kho povidine 10% chai 90ml cuối ngày hôm nay", by_name, stock) is None
    split = "-- type: list\nselect whole, loose from stock_split('Amoxicillin hộp 10 vỉ x 10 viên', 'hộp', date '2026-09-20')"
    assert "'vi'" in frame_advice("tồn của amoxicillin hộp 10 vỉ x 10 viên là mấy vỉ chẵn, mấy viên lẻ", split, f)
    assert frame_advice("tồn của amoxicillin là mấy hộp chẵn, mấy viên lẻ", split, f) is None
    group = Frame(values=["- \"nhóm sỉ\" -> \"customer_group\" = 'sỉ'"])
    assert "customer_group" in frame_advice("tháng này doanh thu từ khách nhóm sỉ", "-- type: money\nselect revenue(date '2026-06-01', date '2026-06-28')", group)
    day = Frame(periods=[("hom qua", "2026-07-07", "2026-07-07")])
    assert "2026-07-07" in frame_advice("doanh thu hôm qua", "-- type: money\nselect revenue(date '2026-07-06', date '2026-07-06')", day)
    assert frame_advice("doanh thu hôm qua", "-- type: money\nselect revenue(date '2026-07-07', date '2026-07-07')", day) is None


def test_table_drops_the_code_column_before_the_name():
    from datamate.sql.agent import to_answer

    got = to_answer("table", ["customer", "customer_name", "debt"], [("KH0027", "Lâm Bảo Ngọc", 632000), ("KH0097", "Nghiêm Duy Linh", 620000)])
    assert got["answer"]["columns"] == ["customer_name", "debt"] and got["answer"]["rows"][0] == ["Lâm Bảo Ngọc", 632000]
    kept = to_answer("table", ["a", "b", "c"], [("X", "Y", 1)])
    assert kept["answer"]["columns"] == ["a", "b", "c"]


def test_dh_is_the_initials_of_don_hang():
    assert rules_normalize("30 ngày gần nhất được bnhiu đh nhỉ?").text == "30 ngày gần nhất được bao nhiêu đơn hàng nhỉ?"
    assert "đơn hàng" in rules_normalize("Tong so dh trong 30 ngay gan nhat la bnhiu?").text
