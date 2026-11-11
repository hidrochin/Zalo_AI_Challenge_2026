import pandas as pd
import pytest

from datamate.ingest.parsers import parse_date, parse_number, parse_unit, year_month_hint
from datamate.ingest.tables import _subtotal_rows


@pytest.mark.parametrize("raw,expected", [
    ("55,000", 55000), ("55.000đ", 55000), ("55k", 55000), ("1,6256tr", 1625600), ("45,000.0000", 45000),
    ("422.400đ", 422400), ("3 chai", 3), ("18.6", 18.6), ("0,9", 0.9), ("-6", -6), (52000, 52000),
    ("1.234.567", 1234567), ("abc", None), ("ORD000001", None), (None, None),
    ("1tr134", 1134000), ("2tr5", 2500000), ("12tr800", 12800000), ("1tr", 1000000),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Không giao được", "huỷ"), ("hàng trả về", "trả hàng"), ("đang gửi", "đang giao"), ("Đổi trả", "trả hàng"),
    ("Đã hoàn thành", "hoàn thành"), ("Hủy đơn", "huỷ"), ("shipping", "đang giao"),
    ("Đang vận chuyển", "đang giao"), ("chuyển khoản", "hoàn thành"), ("Huỳnh", "hoàn thành"),
    ("Đã hủy đơn", "huỷ"), ("khách hủy", "huỷ"),
])
def test_normalize_status(raw, expected):
    from datamate.ingest.roles import normalize_status

    assert normalize_status(raw) == expected


def test_parse_unit():
    assert parse_unit("3 chai") == "chai"
    assert parse_unit("55k") is None


@pytest.mark.parametrize("raw,expected", [
    ("01/02/2025", "2025-02-01"), (45689, "2025-02-01"), ("45689", "2025-02-01"), ("1 thg 1 2025", "2025-01-01"),
    ("2025-02-01 06:00:25", "2025-02-01"), ("31/12/25", "2025-12-31"),
])
def test_parse_date(raw, expected):
    assert parse_date(raw).strftime("%Y-%m-%d") == expected


def test_parse_date_year_hint_and_bad():
    assert parse_date("15/03", year_hint=2026) == pd.Timestamp("2026-03-15")
    assert parse_date("hello") is None
    assert parse_date(12, allow_serial=True) is None


def test_year_month_hint():
    assert year_month_hint("T01_2025") == (2025, 1)
    assert year_month_hint("Thang 02 2025") == (2025, 2)
    assert year_month_hint("04.2025") == (2025, 4)
    assert year_month_hint("Sheet1", "SoBanHang_2026.xlsx") == (2026, None)


def test_header_roles():
    from datamate.ingest.roles import assign_roles

    cols = ["Mã đơn", "Hàng hóa", "S.L", "SL (ĐVT cơ bản)", "Thành tiền (đ)", "Trạng thái đơn", "Khách hàng (tên)", "Giờ tạo đơn"]
    assert assign_roles(cols, {}) == {
        "Mã đơn": "order_id", "Hàng hóa": "product_name", "S.L": "qty", "SL (ĐVT cơ bản)": "qty_base",
        "Thành tiền (đ)": "line_amount", "Trạng thái đơn": "status", "Khách hàng (tên)": "customer_name", "Giờ tạo đơn": "order_ts"}
    # an unmatched description falls back to the header, but never for money roles
    assert assign_roles(["Ngày", "Thành tiền"], {"Ngày": "ngày ghi sổ", "Thành tiền": "tổng tiền đơn"}) == {"Ngày": "order_date"}
    # "số thứ tự dòng trong đơn" is a line number, not the order id
    assert assign_roles(["No.", "Mã bill"], {"No.": "số thứ tự dòng trong đơn", "Mã bill": "mã đơn"}) == {"Mã bill": "order_id"}


def test_subtotal_and_note_rows_dropped():
    from datamate.ingest.tables import RawSheet, extract_table

    raw = pd.DataFrame([
        ["Ngày", "Khách", "Hàng", "Tiền"],
        ["01/03/2025", "Tống Thắng", "Công tắc 1 chiều", "50000"],
        [None, "Tổng ngày 01/03", None, "50000"],
        [None, "Cộng ngày 02/03/2025", None, "10"],
        [None, "TỔNG THÁNG", None, "60"],
        ["Ghi chú: số liệu ghi tay", None, None, None],
        ["02/03/2025", "Tống Thắng Thu", "Ống nước", "10"],
    ])
    df = extract_table(RawSheet("f.xlsx", "T3-2025", raw, False), set())
    assert df["Khách"].tolist() == ["Tống Thắng", "Tống Thắng Thu"]


def test_subtotal_rows_with_a_blank_date_cell_are_dropped():
    df = pd.DataFrame({"khach": ["An", "Tổng ngày 01/01", "Tống Thắng"], "ngay": pd.to_datetime(["2025-01-01", None, "2025-01-02"])})
    assert _subtotal_rows(df).tolist() == [False, True, False]
