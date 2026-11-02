import pandas as pd
import pytest

from datamate import dm
from datamate.agent.hints import time_hints
from datamate.agent.sandbox import check_result
from datamate.canon import build_orders, build_sales, is_walkin, strip_phone
from datamate.ingest.roles import header_key


def calls(q, as_of):
    return [h.split(" -> ")[1].split(" = ")[0] for h in time_hints(q, as_of)]


@pytest.mark.parametrize("q,as_of,expected", [
    ("Doanh thu tháng 5/2026 tăng hay giảm bao nhiêu phần trăm so với tháng trước?", "2026-06-16",
     ["month(2026, 5)", "prev_period(month(2026, 5))"]),
    ("bua qua dthu duoc bao nhieu", "2026-01-10", ["yesterday()"]),
    ("hqua cửa hàng bán được bao nhiêu tiền", "2025-09-04", ["yesterday()"]),
    ("liet ke 3 mat hang hot nhat tuan roi", "2025-03-29", ["week_prev()"]),
    ("dthu thang 1/26 bnhiu", "2026-05-01", ["month(2026, 1)"]),
    ("tháng 11 bán được bao nhiêu", "2026-05-01", ["month(2025, 11)"]),
    ("Cho hỏi hôm 02/07/2025, Gạch thẻ bán ra với đơn giá bao nhiêu", "2026-06-21", ["day('2025-07-02')"]),
    ("so 7 ngày kể từ ngày 28/07/2025 với 7 ngày ngay trước", "2026-06-24",
     ["days_from('2025-07-28', 7)", "day('2025-07-28')", "days_before('2025-07-28', 7)"]),
    ("5 khach con no nhieu nhat tinh den het ngay 27/03/2026", "2026-03-27", ["until('2026-03-27')"]),
    ("dthu từ đầu tháng này đến hết hôm nay so với cùng kỳ th trước", "2026-05-20",
     ["this_month()", "today()", "prev_month_same_days()"]),
    ("so cùng kỳ năm trước, doanh thu tháng 5/2026 tăng mấy %", "2026-06-01",
     ["same_period_last_year(month(2026, 5))", "month(2026, 5)"]),
])
def test_time_hints(q, as_of, expected):
    assert calls(q, as_of) == expected


def test_periods():
    dm.set_context("2026-06-16", None)
    assert dm.prev_period(dm.month(2026, 3)) == dm.month(2026, 2)
    assert dm.prev_period(dm.quarter(2026, 1)) == dm.quarter(2025, 4)
    assert dm.prev_period(dm.days_from("2026-07-02", 14)) == dm.days_before("2026-07-02", 14)
    assert dm.same_period_last_year(dm.month(2024, 2)) == dm.month(2023, 2)


def test_header_key_and_names():
    assert header_key("SLQuyDoi") == "sl quy doi"
    assert header_key("Thành tiền") == "thanh tien"
    assert header_key("ma_kh") == "ma kh"
    assert is_walkin(pd.Series(["kl", "KL", "Khách lẻ", "Kim Lan"])).tolist() == [True, True, True, False]
    assert strip_phone("Dương Hùng Trung - 0328 548 455") == "Dương Hùng Trung"
    assert strip_phone("Bia 330ml") == "Bia 330ml"


def test_orders_rollup():
    lines = pd.DataFrame({"ma": ["A", "A", "B"], "tien": [100, 250, 70], "sl": [1, 2, 1], "ten": ["x", "y", "x"],
                          "ngay": pd.to_datetime(["2025-01-01"] * 3), "kenh": ["zalo", "zalo", "quầy"]})
    roles = {"lines": {"ma": "order_id", "tien": "line_amount", "sl": "qty", "ten": "product_name", "ngay": "order_date"}}
    s, desc = build_sales({"lines": lines}, roles)
    assert "kenh" in s.columns and "kenh" in desc
    o = build_orders(s).set_index("order_id")
    assert o.loc["A", "goods_amount"] == 350 and o.loc["A", "n_lines"] == 2 and o.loc["B", "kenh"] == "quầy"


def test_check_result():
    names = {"bia tiger lon"}
    assert check_result({"type": "string", "answer": "True"}, names)[0]
    assert check_result({"type": "string", "answer": "Bia Tiger lon"}, names) == (None, None)
    assert check_result({"type": "list", "answer": [True]}, names)[0]
    assert check_result({"type": "unanswerable", "reason": "No sales data in Oct 2025", "_src": "model"}, names)[0]
    assert check_result({"type": "unanswerable", "reason": "dữ liệu không có cột giới tính", "_src": "model"}, names) == (None, None)
    assert check_result({"type": "number", "answer": 0}, names)[1]
    for reason in ["sales_lines() is not available for this shop; use T directly", "Không có helper orders() cho shop này"]:
        assert check_result({"type": "unanswerable", "reason": reason, "_src": "model"}, names)[0]
    assert check_result({"type": "unanswerable", "reason": "Dữ liệu không có cột ghi lý do huỷ đơn.", "_src": "model"}, names) == (None, None)


def test_llm_retry_then_stop(monkeypatch):
    from datamate.agent import pipeline

    class Down:
        calls = 0

        def generate(self, *a, **kw):
            Down.calls += 1
            raise ConnectionError("refused")

    class Rejected(Exception):
        status_code = 400

    class Bad:
        def generate(self, *a, **kw):
            raise Rejected("prompt too long")

    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    pipeline._LLM_DOWN.clear()
    cfg = pipeline.RunConfig(llm_retries=2)
    with pytest.raises(Rejected):  # a 4xx is the question's own error: no retry, the run goes on
        pipeline._generate(Bad(), cfg)
    assert not pipeline._LLM_DOWN.is_set()
    with pytest.raises(pipeline.LLMUnavailable):
        pipeline._generate(Down(), cfg)
    assert Down.calls == 3 and pipeline._LLM_DOWN.is_set()
    pipeline._LLM_DOWN.clear()
