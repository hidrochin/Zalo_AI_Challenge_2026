import pandas as pd

from datamate.canon import build_sales, is_walkin


def test_counted_rule():
    lines = pd.DataFrame({
        "ma": ["A", "A", "B", "C", "C", "D"],
        "tien": [100, 50, 70, 30, -10, 40],
        "sl": [1, 1, 1, 1, None, -1],
        "ten": ["x", "y", "x", "x", "Voucher", "x"],
    })
    orders = pd.DataFrame({
        "ma": ["A", "B", "C", "D"],
        "ngay": pd.to_datetime(["2025-01-01"] * 4),
        "tt__norm": ["hoàn thành", "huỷ", "đang giao", "hoàn thành"],
        "tt": ["xong", "huỷ", "đang giao", "xong"],
    })
    roles = {
        "lines": {"ma": "order_id", "tien": "line_amount", "sl": "qty", "ten": "product_name"},
        "orders": {"ma": "order_id", "ngay": "order_date", "tt": "status"},
    }
    s, _ = build_sales({"lines": lines, "orders": orders}, roles)
    # A counts; B cancelled; C in delivery counts but its voucher line does not; D has a returned line
    assert s.loc[s["counted"], "amount"].sum() == 100 + 50 + 30


def test_is_walkin():
    assert is_walkin(pd.Series(["Khách lẻ", "lẻ", None, "Nguyễn Văn A", "khach vang lai"])).tolist() == [True, True, True, False, True]
