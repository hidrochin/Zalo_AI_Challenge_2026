from pathlib import Path

import pandas as pd
import pytest

from datamate import dm
from datamate.agent.prompt import extract_code
from datamate.agent.vote import decide

CAL = Path("data/train/calendar.csv")


@pytest.fixture(autouse=True)
def ctx():
    cal = None
    if CAL.exists():
        cal = pd.read_csv(CAL)
        cal["date"] = pd.to_datetime(cal["date"])
    dm.set_context("2026-03-04", cal)  # a Wednesday


def test_periods():
    assert dm.week_prev() == (pd.Timestamp("2026-02-23"), pd.Timestamp("2026-03-01"))
    assert dm.week_this() == (pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-04"))
    assert dm.this_month().start == pd.Timestamp("2026-03-01")
    assert dm.prev_month_same_days() == (pd.Timestamp("2026-02-01"), pd.Timestamp("2026-02-04"))
    assert dm.month_auto(5).start.year == 2025
    assert dm.last_n_days(7).start == pd.Timestamp("2026-02-26")
    assert dm.quarter(2026, 2).end == pd.Timestamp("2026-06-30")


def test_prev_month_cap():
    dm.set_context("2026-03-31", None)
    assert dm.prev_month_same_days().end == pd.Timestamp("2026-02-28")


@pytest.mark.skipif(not CAL.exists(), reason="needs data/train/calendar.csv")
def test_tet_2026():
    t = dm.tet(2026)
    assert t.start == pd.Timestamp("2026-02-10")  # 23 tháng Chạp Ất Tỵ
    assert t.end == pd.Timestamp("2026-02-26")  # mùng 10 tháng Giêng Bính Ngọ
    assert dm.pre_tet(2026).end == pd.Timestamp("2026-02-09")


def test_in_period_clips_to_as_of():
    s = pd.Series(pd.to_datetime(["2026-03-01", "2026-03-05"]))
    assert dm.in_period(s, dm.month(2026, 3)).tolist() == [True, False]


def test_answers_and_rounding():
    assert dm.answer_number(10.5, kind="money")["answer"] == 11
    assert dm.answer_percent(-3.775)["answer"] == -3.78
    assert dm.answer_list([1.0, 2.5])["answer"] == [1, 2.5]


def test_rank_top_tie_by_code():
    df = pd.DataFrame({"code": ["B", "A", "C"], "qty": [5, 5, 1]})
    assert dm.rank_top(df, "qty", 2, code="code")["code"].tolist() == ["A", "B"]


def test_find_without_diacritics():
    assert dm.find(["Nước suối Lavie 1.5L", "Coca Cola 330ml"], "nuoc suoi lavie 1.5l")[0] == "Nước suối Lavie 1.5L"


def test_extract_code_and_vote():
    assert extract_code("plan\n```python\nx=1\nanswer_number(x)\n```") == "x=1\nanswer_number(x)\n"
    r = {"type": "number", "answer": 5}
    assert decide([r, r, None])[0] == r
    assert decide([r, {"type": "number", "answer": 6}, None])[0] == {"type": "abstain"}


def test_avg_order_value_counts_orders_not_lines():
    sales = pd.DataFrame({
        "date": pd.to_datetime(["2025-07-01", "2025-07-01", "2025-07-02", "2025-07-03", "2025-08-01"]),
        "order_id": ["A", "A", "B", "C", "D"],
        "counted": [True, True, True, False, True],  # C cancelled, D outside July
        "amount": [100.0, 50.0, 101.0, 999.0, 7.0],
    })
    dm.set_context("2025-08-31", None, sales)
    assert dm.avg_order_value(dm.month(2025, 7)) == 126  # (150 + 101) / 2 orders = 125.5 -> half-up


def test_period_against_date_columns():
    s = pd.Series(pd.to_datetime(["2025-10-19", "2025-10-20", "2025-10-26 15:30", "2025-10-27"], format="mixed"))
    p = dm.between(dm.day("2025-10-20"), dm.day("2025-10-26"))  # periods where dates are expected
    assert p == (pd.Timestamp("2025-10-20"), pd.Timestamp("2025-10-26"))
    assert p["start"] == p.start and p[1] == p.end
    assert (s == p).tolist() == [False, True, True, False]
    assert (s.dt.date == p).tolist() == [False, True, True, False]  # python dates too
    assert (s >= p).tolist() == [False, True, True, True]
    assert (s <= p).tolist() == [True, True, True, False]
    assert (s > p).tolist() == [False, False, False, True]
    assert (s != dm.day("2025-10-20")).tolist() == [True, False, True, True]
    assert dm.until(dm.day("2025-09-30")).end == pd.Timestamp("2025-09-30")
    start, end = p
    assert end - start == pd.Timedelta(days=6)


def test_rank_top_sums_repeated_keys():
    lines = pd.DataFrame({"product_id": ["B", "A", "B", "C", "A"], "product_name": ["Bia", "Coca", "Bia", "Mì", "Coca"],
                          "qty_base": [2, 3, 2, 9, 1], "order_id": [1, 2, 3, 4, 5]})
    top = dm.rank_top(lines, by="qty_base", k=2, code="product_id")
    assert top["product_id"].tolist() == ["C", "A"]  # A: 4 ties B: 4, code ascending
    assert top["product_name"].tolist() == ["Mì", "Coca"] and top["qty_base"].tolist() == [9, 4]
    assert "order_id" not in top  # differs within a product


def test_helpers_take_a_row_mask():
    sales = pd.DataFrame({
        "date": pd.to_datetime(["2025-07-01", "2025-07-02", "2025-07-03"]),
        "order_id": ["A", "B", "C"], "counted": [True, True, True], "amount": [100.0, 50.0, 7.0], "staff": ["x", "y", "x"],
    })
    dm.set_context("2025-08-31", None, sales)
    s = dm.sales_lines()
    assert dm.revenue(dm.month(2025, 7), s["staff"] == "x") == 107
