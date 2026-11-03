import pytest

from datamate.postprocess import check_answer, date_window, expect_answer, finalize, validate_submission


@pytest.mark.parametrize("q,typ,month,k", [
    ("Trong 3 tháng 2/2025, tháng 3/2025, tháng 4/2025, tháng nào doanh thu cao nhất?", "date", True, None),
    ("Tháng 2/2025 ngày nào doanh thu cao nhất?", "date", False, None),
    ("Tháng 5/2026 tỉ lệ đơn bị huỷ là bao nhiêu %?", "percent", None, None),
    ("Top 3 sản phẩm bán chạy nhất tháng 1/2026", "list", None, 3),
    ("Liệt kê 5 khách nợ nhiều nhất đến hết ngày 30/06/2026, kèm số tiền còn nợ", "table", None, 5),
    ("Tháng 7/2025 bán được bao nhiêu chai Efticol 0,9% lốc 10 lọ?", None, None, None),
])
def test_expect_rules(q, typ, month, k):
    e = expect_answer(q)
    assert (e.type, e.month, e.k) == (typ, month, k)
    assert e.hard == (typ is not None)


def test_expect_llm_is_soft():
    e = expect_answer("Doanh thu tháng 5/2026 có tăng không?", {"answer_type": "boolean"})
    assert (e.type, e.hard) == ("boolean", False)
    e = expect_answer("Tháng 5/2026 tỉ lệ huỷ là bao nhiêu?", {"answer_type": "number"})
    assert (e.type, e.hard) == ("percent", False)


def test_check_answer():
    e = expect_answer("Trong 3 tháng 3/2025, tháng 4/2025, tháng 5/2025, tháng nào doanh thu cao nhất?")
    assert check_answer({"type": "list", "answer": [1, 2, 3]}, e)[1] is True
    assert check_answer({"type": "date", "answer": "2025-04-30"}, e)[0]
    assert check_answer({"type": "date", "answer": "2025-04"}, e) == (None, False)
    e = expect_answer("Top 3 sản phẩm bán chạy nhất tháng 1/2026")
    assert check_answer({"type": "list", "answer": ["a", "b", "c", "d"]}, e)[1] is True
    assert check_answer({"type": "list", "answer": ["a", "b"]}, e) == (None, False)
    assert check_answer({"type": "unanswerable", "reason": "x"}, e) == (None, False)
    e = expect_answer("Doanh thu tháng 5/2026 có tăng không?", {"answer_type": "boolean"})
    assert check_answer({"type": "number", "answer": 3}, e)[1] is False  # the rewrite's guess is soft


def test_date_window():
    hints = ['- "tháng 9/2025" -> month(2025, 9) = 2025-09-01..2025-09-30',
             '- "tháng 11/2025" -> month(2025, 11) = 2025-11-01..2025-11-30']
    assert date_window("Từ tháng 9/2025 đến tháng 11/2025, tháng nào doanh thu cao nhất?", hints) == ("2025-09-01", "2025-11-30")
    assert date_window("Lô X gần hết hạn nhất có hạn dùng ngày nào?", hints) is None
    e = expect_answer("Doanh thu ngày nào cao nhất trong tháng 10 năm 2025?")
    e.window = ("2025-10-01", "2025-10-31")
    assert check_answer({"type": "date", "answer": "2025-11-01"}, e)[1] is True
    assert check_answer({"type": "date", "answer": "2025-10-31"}, e) == (None, False)


@pytest.mark.parametrize("final,out", [
    ({"type": "number", "answer": 1500.0}, {"type": "number", "answer": 1500}),
    ({"type": "percent", "answer": -0.0}, {"type": "percent", "answer": 0.0}),
    ({"type": "percent", "answer": 12.345678}, {"type": "percent", "answer": 12.35}),
    ({"type": "number", "answer": float("nan")}, {"type": "abstain"}),
    ({"type": "number", "answer": True}, {"type": "abstain"}),
    ({"type": "date", "answer": "2025-4-1"}, {"type": "abstain"}),
    ({"type": "list", "answer": []}, {"type": "abstain"}),
    ({"type": "list", "answer": ["Bia  Tiger "]}, {"type": "list", "answer": ["Bia Tiger"]}),
    ({"type": "boolean", "answer": "true"}, {"type": "abstain"}),
    ({"type": "string", "answer": "  "}, {"type": "abstain"}),
    ({"type": "unanswerable", "reason": "no gender column", "_src": "model"},
     {"type": "unanswerable", "reason": "no gender column"}),
    ({"type": "table", "answer": {"columns": ["khach_hang", "so_tien_no"], "rows": [["A", "B", "C"], [3, 2, 1]]}},
     {"type": "table", "answer": {"columns": ["khach_hang", "so_tien_no"], "rows": [["A", 3], ["B", 2], ["C", 1]]}}),
    ({"type": "table", "answer": {"columns": ["a", "b"], "rows": [[]]}}, {"type": "abstain"}),
])
def test_finalize(final, out):
    assert finalize(final) == out


def test_validate_submission():
    lines = [{"id": "a", "type": "number", "answer": 3}, {"id": "a", "type": "abstain"},
             {"id": "b", "type": "list", "answer": []}, {"id": "z", "type": "unanswerable", "reason": "x"}]
    rep = validate_submission(lines, ["a", "b", "c"])
    assert rep["duplicates"] == ["a"] and rep["unknown_ids"] == ["z"] and rep["missing_ids"] == 1
    assert rep["malformed"] == ["b"]
