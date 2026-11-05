import pandas as pd

from datamate.canon import build_sales, busy_customers, is_walkin


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


def test_unit_factors_nested_and_base_default():
    lines = pd.DataFrame({
        "ma": ["A", "A", "B", "B"],
        "ngay": pd.to_datetime(["2025-01-01"] * 4),
        "ten": ["Panadol hộp 10 vỉ x 12 viên"] * 3 + ["Gạo"],
        "dvt": ["hộp", "vỉ", "viên", "kg"],
        "sl": [1, 2, 3, 5],
        "tien": [100, 20, 3, 50],
    })
    conv = pd.DataFrame({
        "Tên sản phẩm": ["Panadol hộp 10 vỉ x 12 viên"] * 2,
        "Đơn vị quy đổi": ["vỉ", "hộp"],
        "Tỉ lệ quy đổi": [12, 10],
        "Tỉ lệ quy đổi__unit": [None, "vỉ"],  # 1 hộp = 10 vỉ (= 120 viên)
        "Đơn vị cơ bản": ["viên", "viên"],
    })
    roles = {
        "lines": {"ma": "order_id", "ngay": "order_date", "ten": "product_name", "dvt": "unit", "sl": "qty", "tien": "line_amount"},
        "DanhSachQuyDoi": {"Tên sản phẩm": "product_name", "Đơn vị cơ bản": "base_unit"},
    }
    s, desc = build_sales({"lines": lines, "DanhSachQuyDoi": conv}, roles)
    assert s["qty_base"].tolist() == [120, 24, 3, 5]  # no conversion row (viên, kg) = base unit
    assert "unit factor" in desc


def customers(busiest: str, n: int = 200) -> pd.DataFrame:
    """One customer with 30 orders, n others with 1 each (all named)."""
    names = [busiest] * 30 + [f"Khách số {i}" for i in range(n)]
    return pd.DataFrame({"name": names, "order": range(len(names)), "walkin": False})


def test_busy_name_with_a_walkin_word_is_a_label():
    df = customers("Khách mua lẻ")
    assert busy_customers(df["name"], df["walkin"], df["order"]) == ({"khach mua le"}, [])


def test_busy_name_without_a_walkin_word_is_only_flagged():
    df = customers("Công ty Xây dựng An Phát")
    assert busy_customers(df["name"], df["walkin"], df["order"]) == (set(), ["cong ty xay dung an phat"])


def test_ordinary_customers_are_left_alone():
    df = pd.DataFrame({"name": ["Lê Hiếu Ngân", "Lê Hiếu Ngân", "An", "Bình"], "order": [1, 2, 3, 4], "walkin": False})
    assert busy_customers(df["name"], df["walkin"], df["order"]) == (set(), [])


def test_build_sales_treats_an_unknown_walkin_label_as_walkin():
    df = customers("Khách mua lẻ").assign(ngay=pd.Timestamp("2025-01-01"), tien=100, sl=1)
    roles = {"t": {"order": "order_id", "ngay": "order_date", "tien": "line_amount", "sl": "qty", "name": "customer_name"}}
    s, _ = build_sales({"t": df}, roles)
    assert s["walkin"].sum() == 30 and s.loc[s["walkin"], "customer"].isna().all()
    assert s["counted"].all()  # still sales
