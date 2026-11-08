import pandas as pd

from datamate import dm
from datamate.canon import build_sales
from datamate.facts import build_facts
from datamate.ingest.business import Business, TableInfo
from datamate.ingest.readme import parse_readme, unit_rules


def _shop():
    lines = pd.DataFrame({"Mã đơn": ["A", "B", "C", "D"], "Mã hàng": ["P1"] * 4, "Tên hàng": ["Sơn"] * 4, "Thành tiền": [100, 200, 300, 400],
                          "SL": [1, 2, 3, 4], "Giá vốn": [10, 10, 10, 10]})
    orders = pd.DataFrame({"Mã đơn": ["A", "B", "C", "D"], "Ngày": pd.to_datetime(["2025-01-01", "2025-01-10", "2025-02-01", "2025-02-05"]),
                           "Khách": ["Lan", "Lan", "Hùng", "kl"], "Tổng sau giảm giá": [100, 200, 300, 400],
                           "Phương thức thanh toán": ["Ghi nợ", "Ghi nợ", "Thu hộ (COD)", "Tiền mặt"], "TT": ["xong"] * 4, "TT__norm": ["hoàn thành"] * 4})
    pay = pd.DataFrame({"Mã đơn": ["A", "A", "B", "D"], "Thời gian": pd.to_datetime(["2025-01-01", "2025-01-20", "2025-02-02", "2025-02-05"]),
                        "Giá trị": [40, 30, 50, 400]})
    trips = pd.DataFrame({"Mã đơn": ["A", "A", "A", "B"], "Lượt giao": [1, 1, 2, 1], "Ngày giao": pd.to_datetime(["2025-01-02", "2025-01-02", "2025-01-03", "2025-02-01"])})
    roles = {"lines": {"Mã đơn": "order_id", "Mã hàng": "product_id", "Tên hàng": "product_name", "Thành tiền": "line_amount", "SL": "qty"},
             "orders": {"Mã đơn": "order_id", "Ngày": "order_date", "Khách": "customer_name", "TT": "status"},
             "pay": {"Mã đơn": "order_id"}, "trips": {"Mã đơn": "order_id"}}
    tables = {"lines": lines, "orders": orders, "pay": pay, "trips": trips}
    biz = Business("x", "train", parse_readme(""), tables, {n: TableInfo(n, [], {}, r) for n, r in roles.items()}, [])
    sales, _ = build_sales(tables, roles)
    return sales, build_facts(biz, sales)


def test_debt_counts_what_is_unpaid_at_the_date_and_skips_cod():
    sales, facts = _shop()
    dm.set_context("2025-03-01", None, sales, facts)
    assert dm.debt_total("2025-01-10") == 60 + 200  # A: 100 - 40, B: 200; C is COD and later
    assert dm.debt_total("2025-03-01") == 30 + 150
    assert dm.debt_total(dm.day("2025-01-10")) == dm.debt_total("2025-01-10")
    top = dm.debt_customers()
    assert top["customer_name"].tolist() == ["Lan"] and top["debt"].tolist() == [180]
    assert dm.debt_orders("2025-01-11").set_index("order_id")["age"].to_dict() == {"A": 10, "B": 1}


def test_repaid_excludes_the_day_of_purchase_and_new_debt_excludes_it_from_the_debt():
    sales, facts = _shop()
    dm.set_context("2025-03-01", None, sales, facts)
    assert dm.debt_repaid(dm.month(2025, 1)) == 30
    assert dm.debt_repaid(dm.month(2025, 2)) == 50  # on an order of January
    assert dm.debt_new(dm.month(2025, 1)) == 60 + 200


def test_trips_count_pairs_not_lines_and_profit_multiplies_cost_by_quantity():
    sales, facts = _shop()
    dm.set_context("2025-03-01", None, sales, facts)
    assert dm.delivery_trips(dm.month(2025, 1)) == 2
    assert dm.gross_profit(dm.month(2025, 1)) == 300 - 10 * 3


def test_a_fact_the_helper_cannot_see_is_not_a_verdict_of_unanswerable():
    sales, facts = _shop()
    dm.set_context("2025-03-01", None, sales, facts)
    for call in (lambda: dm.stock("Sơn"), lambda: dm.imports(), lambda: dm.net_profit(dm.month(2025, 1))):
        try:
            call()
        except dm.NoFact:
            continue
        raise AssertionError("expected NoFact")


def test_unit_rules_read_both_prose_forms():
    text = "- **Hàng theo bó**: Ống D32, Ống D50 bán theo bó, 1 bó = 10 cây; ghi số bó.\n  - Đá 1x2: 1 xe 4m3 = 4 m3\n- Dữ liệu có đến hết 01/01/2026."
    assert unit_rules(text) == [("Ống D32", "bó", 10.0, "cây"), ("Ống D50", "bó", 10.0, "cây"), ("Đá 1x2", "xe 4m3", 4.0, "m3")]
