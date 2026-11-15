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


def test_description_column_is_not_a_lookup_sheet():
    lines = pd.DataFrame({"Mã": ["1", "2", "3"], "Ngày": pd.to_datetime(["2025-01-01"] * 3), "Hàng": ["Trà", "Cà phê", "Sữa"], "Tiền": [1, 2, 3], "SL": [1, 1, 1]})
    master = pd.DataFrame({"Hàng": ["Trà", "Cà phê", "Sữa"], "Mô tả": ["trà ngon", "cà phê đậm", "sữa tươi"]})
    roles = {"lines": {"Mã": "order_id", "Ngày": "order_date", "Hàng": "product_name", "Tiền": "line_amount", "SL": "qty"}, "m": {"Hàng": "product_name"}}
    s, _ = build_sales({"lines": lines, "m": master}, roles)
    assert s["product_name"].tolist() == ["Trà", "Cà phê", "Sữa"]


def test_group_of_the_product_list_is_no_customer_group():
    lines = pd.DataFrame({"Mã": ["1", "2", "3"], "Ngày": pd.to_datetime(["2025-01-01"] * 3), "Hàng": ["Bỉm", "Sữa", "Bỉm"],
                          "Tiền": [1, 2, 3], "SL": [1, 1, 1], "Khách": ["An", "Bình", "An"]})
    master = pd.DataFrame({"Hàng": ["Bỉm", "Sữa"], "Nhóm": ["bỉm tã", "sữa bột"]})
    roles = {"lines": {"Mã": "order_id", "Ngày": "order_date", "Hàng": "product_name", "Tiền": "line_amount", "SL": "qty",
                       "Khách": "customer_name"}, "m": {"Hàng": "product_name"}}
    s, _ = build_sales({"lines": lines, "m": master}, roles)
    assert "customer_group" not in s.columns and s["Nhóm"].tolist() == ["bỉm tã", "sữa bột", "bỉm tã"]
    s, _ = build_sales({"lines": lines.assign(**{"Nhóm khách": ["sỉ", "lẻ", "sỉ"]})}, roles)
    assert s["customer_group"].tolist() == ["sỉ", "lẻ", "sỉ"]  # on the order itself it still is


def test_order_only_shop_reads_each_order_as_one_line():
    orders = pd.DataFrame({"Mã": ["A", "B"], "Ngày": pd.to_datetime(["2025-01-01", "2025-01-02"]), "Tiền hàng": [100, 200],
                           "TT": ["xong", "huỷ"], "TT__norm": ["hoàn thành", "huỷ"]})
    s, _ = build_sales({"o": orders}, {"o": {"Mã": "order_id", "Ngày": "order_date", "TT": "status"}})
    assert s.loc[s["counted"], "amount"].sum() == 100


def test_order_only_shop_finds_the_goods_value_by_its_readme_description():
    from datamate.ingest.roles import assign_roles

    # biz_110's README sentence for `Tien hang`, under a header no rule knows
    descs = {"Giá trị đơn": "tiền hàng của đơn: cộng thành tiền các mặt hàng, trước giảm giá của đơn, chưa gồm phí giao",
             "Mã": "mã đơn", "Ngày": "ngày bán", "TT": "trạng thái đơn"}
    roles = assign_roles(["Mã", "Ngày", "Giá trị đơn", "Tổng", "TT"], descs)
    assert roles["Giá trị đơn"] == "order_goods"
    orders = pd.DataFrame({"Mã": ["A", "B"], "Ngày": pd.to_datetime(["2025-01-01", "2025-01-02"]),
                           "Giá trị đơn": [100, 200], "Tổng": [110, 220], "TT": ["xong", "xong"],
                           "TT__norm": ["hoàn thành", "hoàn thành"]})
    s, _ = build_sales({"o": orders}, {"o": roles})
    assert s["amount"].sum() == 300


def test_boolean_cells_stay_boolean():
    from datamate.ingest.business import TableInfo, _type_columns
    df = pd.DataFrame({"Tên": ["A", "B", "C"], "Kê đơn": [True, "False", "TRUE"], "Ghi chú": ["True", "khác", None],
                       "_file": ["f.xlsx"] * 3, "_sheet": ["s"] * 3})
    out = _type_columns(df, ["Tên", "Kê đơn", "Ghi chú"], TableInfo("t", [("f.xlsx", None)], {}, {}))
    assert out["Kê đơn"].dtype == bool and out["Kê đơn"].tolist() == [True, False, True]
    assert out["Ghi chú"].iloc[:2].tolist() == ["True", "khác"] and out["Ghi chú"].isna().iloc[2]  # free text stays text


def test_service_fee_stays_in_revenue_but_is_not_an_item():
    from datamate.canon import drop_fee_items

    lines = pd.DataFrame({"product_name": ["Bia Tiger lon", "Phí phục vụ"], "qty": [2, 1], "qty_base": [2.0, 1.0], "amount": [40000, 5000],
                          "counted": [True, True]})
    assert drop_fee_items(lines) == 1
    assert lines["product_name"].isna().tolist() == [False, True] and pd.isna(lines.loc[1, "qty_base"])
    assert lines.loc[lines["counted"], "amount"].sum() == 45000
