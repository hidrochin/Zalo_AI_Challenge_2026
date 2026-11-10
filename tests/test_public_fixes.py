"""Deterministic fixes from the public-test error attribution (debug/sql_err_public/REPORT.md): each reads content, never ids."""
import pandas as pd
import pytest

from datamate.agent.hints import time_hints
from datamate.agent.lint import catalog_group
from datamate.canon import build_orders, customer_group_col
from datamate.facts import note_payments
from datamate.ingest.roles import normalize_status
from datamate.postprocess import fit_money, invariants
from datamate.preprocess import EntityIndex, rules_normalize
from datamate.sql.agent import order_money_lint


def test_fit_money_rounds_money_only():
    assert fit_money({"type": "number", "answer": 505280.91}, "Tuần trước doanh thu bình quân trên 1 đơn hàng là bao nhiêu?")["answer"] == 505281
    assert fit_money({"type": "number", "answer": 13.5}, "Trung bình mỗi thứ Tư tiệm sửa mấy lượt xe?")["answer"] == 13.5
    assert fit_money({"type": "number", "answer": 2.5}, "Bán được bao nhiêu ly trà sữa?")["answer"] == 2.5


def test_money_unit_skips_amount_filters():
    q = "Đếm giùm tháng 5/2025 có mấy đơng tiền hàng từ 760.000đ trở lên nha"
    assert not invariants({"type": "number", "answer": 211}, q)
    assert invariants({"type": "number", "answer": 211}, "Doanh thu tháng 5/2025 là bao nhiêu?")


def test_customer_group_by_header_and_values():
    t = pd.DataFrame({"Đối tượng KH": ["Cá nhân"] * 4, "Kiểu khách": ["lẻ", "sỉ", "thân thiết", "lẻ"], "Tên": list("abcd")})
    assert customer_group_col(t) == "Kiểu khách"
    t = pd.DataFrame({"Loại khách": ["sỉ", "lẻ", "thân thiết"], "Khách lẻ/sỉ": ["An", "Bình", "Chi"]})
    assert customer_group_col(t) == "Loại khách"


def test_orders_keep_only_order_fields():
    s = pd.DataFrame({"order_id": [1, 1, 2], "date": pd.to_datetime(["2025-01-01"] * 3), "status": ["hoàn thành"] * 3,
                      "order_ok": True, "counted": True, "is_goods": True, "amount": [10, 20, 30],
                      "Nhóm hàng": ["bia", "nước", "bia"], "Bớt": [5, None, 0]})
    o = build_orders(s)
    assert "Nhóm hàng" not in o.columns and "Bớt" in o.columns and "status" in o.columns


def test_debt_from_order_notes():
    o = pd.DataFrame({"order_id": ["A", "B", "C", "D"], "date": pd.to_datetime(["2026-07-01"] * 4),
                      "total": [286000.0, 337500.0, 500000.0, 100000.0], "on_credit": [True, True, True, False]})
    notes = pd.Series({"A": "đưa trước 236k, còn lại ghi sổ", "B": "nợ cả đơn", "C": "nợ 69 nghìn, khách ck đủ rồi"})
    p = note_payments(o, notes, "2026-07-22")
    paid = p.groupby("order_id")["amount"].sum()
    assert paid["A"] == 236000 and "B" not in paid and paid["C"] == 500000 and paid["D"] == 100000
    assert p[(p["order_id"] == "C") & (p["date"] == "2026-07-22")]["amount"].iloc[0] == 69000


def test_checkup_note_is_no_return():
    assert normalize_status("kiểm tra lại tuần sau") == "hoàn thành"
    assert normalize_status("khách trả lại") == "trả hàng"


def test_month_list_takes_the_year_at_its_end():
    hints = time_hints(rules_normalize("Doanh thu tháng 3, tháng 4, tháng 5 năm 2025 lần lượt bao nhiêu?").text, "2026-07-22")
    assert [h.split(" -> ")[1].split(" =")[0] for h in hints] == ["month(2025, 3)", "month(2025, 4)", "month(2025, 5)"]
    hints = time_hints("Tháng 5 doanh thu so với tháng 4/2025", "2026-07-01")
    assert hints[0].split(" -> ")[1].startswith("month(2026, 5)")


def test_from_the_first_of_the_month():
    hints = time_hints(rules_normalize("dthu tu mung 1 toi het hom nay so vs cung ky thang trc").text, "2025-08-08")
    calls = [h.split(" -> ")[1].split(" =")[0] for h in hints]
    assert "this_month()" in calls and "prev_month_same_days()" in calls


def test_northern_dish_words_match_southern_names():
    ix = EntityIndex({"product": {"Cháo nghêu", "Đậu hũ chiên giòn", "Bia Tiger lon"}})
    assert rules_normalize("Cháo ngao bán được mấy nồi?", ix).text.startswith("Cháo nghêu")
    assert "Đậu hũ chiên giòn" in rules_normalize("Tiền bán Đậu phụ rán giòn tháng 10/2025", ix).text
    assert [m.name for m in rules_normalize("bán mấy lon bia tiger lon", ix).mentions] == ["Bia Tiger lon"]  # 'lon' is no 'lợn'


@pytest.mark.parametrize("q,out", [("tuần trc ca chiều dduowcj mí đơn tổg cộg zị ha", "mấy đơn tổng cộng"),
                                   ("dthu các đơn trả xi ô đi tháng 3", "COD"),
                                   ("Cuối ngày hôm nay có mấy mặt hàng đang cháy hàng?", "hết hàng")])
def test_slang(q, out):
    assert out in rules_normalize(q).text


def test_order_money_lint():
    code = 'select sum("Thu máy cũ") from sales_lines where counted'
    assert order_money_lint(code, ("Thu máy cũ",))
    assert not order_money_lint('select sum("Thu máy cũ") from orders where counted', ("Thu máy cũ",))


@pytest.mark.parametrize("q,group", [
    ("T5/2025 co may mat hang nam trong it nhat 122 don khac nhau?", "items_min_orders"),
    ("Khách mua từ 12 đơn trở lên T4/2026, chiếm bnhiu % số khách có mua tháng đó?", "customers_min_orders"),
    ("Tuan truoc dthu ca chieu hon ca sang bnhiu % nhi?", "shift"),
    ("Tháng 3/2025, trong các khách hàng, ai trung thành nhất?", "customer_rank"),
    ("T1/2026 tới T3/2026, tháng nào quán bán đc nhiều tiền nhất?", "month_rank"),
])
def test_catalog_groups(q, group):
    macros = {"items_in_min_orders", "share_customers_min_orders", "shift_revenue", "customer_stats", "month_stats"}
    assert catalog_group(rules_normalize(q).text, macros)[0] == group
