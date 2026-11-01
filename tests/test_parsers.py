import pandas as pd
import pytest

from datamate.ingest.parsers import parse_date, parse_number, parse_unit, year_month_hint


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
