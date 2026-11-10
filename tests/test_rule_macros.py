"""Rulebook macros added 2026-11-11 (revenue_of, share_of, revenue_rising, ...), the query rewrites that keep the model's
macro calls runnable, the unit check, the value hints and the conversion rules written in a cell."""

import pandas as pd
import pytest

from datamate.canon import _written_rules
from datamate.sql.agent import free_text_lint, rewrite_macros, unit_asked, unit_shadow
from datamate.sql.store import build_store, connect, run_select
from datamate.sql.values import ValueIndex, subjective_note


def shop():
    lines = pd.DataFrame({
        "ma": ["A", "A", "B", "C", "D", "E"],
        "tien": [100, 50, 70, 30, 40, 1000],
        "sl": [1, 1, 1, 1, -1, 1],
        "ten": ["Gạo", "Gạo", "Gạo", "Muối", "Gạo", "Muối"],
        "Kênh bán": ["store", "store", "store", "zalo_oa", "store", "store"],
        "Kê đơn": [True, True, False, False, False, True],
    })
    orders = pd.DataFrame({
        "ma": ["A", "B", "C", "D", "E"],
        "ngay": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-02-01"]),
        "tt": ["xong", "huỷ", "xong", "xong", "xong"],
        "tt__norm": ["hoàn thành", "huỷ", "hoàn thành", "hoàn thành", "hoàn thành"],
    })
    roles = {"lines": {"ma": "order_id", "tien": "line_amount", "sl": "qty", "ten": "product_name"},
             "orders": {"ma": "order_id", "ngay": "order_date", "tt": "status"}}
    return {"lines": lines, "orders": orders}, roles


@pytest.fixture
def con(tmp_path):
    tables, roles = shop()
    path = tmp_path / "shop.duckdb"
    build_store(path, tables, roles)
    c = connect(path, "2025-02-28")
    yield c
    c.close()


def one(con, sql):
    return run_select(con, sql)[1][0][0]


JAN = "date '2025-01-01', date '2025-01-31'"


def test_part_of_the_sales_by_one_column(con):
    # counted in January: A (100 + 50, store) and C (30, zalo); B is cancelled, D has a returned line
    assert one(con, f"select revenue_of(\"Kênh bán\", 'store', {JAN})") == 150
    assert one(con, f"select revenue_of(\"Kênh bán\", 'Zalo OA', {JAN})") == 30  # same_text: '_' is a space, case ignored
    assert one(con, f"select share_of(\"Kênh bán\", 'store', {JAN})") == 83.33  # of the whole shop's revenue
    assert one(con, f"select orders_of(\"Kênh bán\", 'store', {JAN})") == 1
    assert one(con, f"select revenue_of(flag(\"Kê đơn\"), true, {JAN})") == 150
    rows = run_select(con, f"select \"value\", revenue, orders from group_stats(\"Kênh bán\", {JAN})")[1]
    assert rows == [("store", 150, 1), ("zalo_oa", 30, 1)]


def test_revenue_rising_compares_each_month_with_the_one_before(con):
    assert one(con, "select revenue_rising(date '2025-01-01', date '2025-02-28')") is True  # 180 then 1000
    assert one(con, "select revenue_rising(date '2024-12-01', date '2025-01-31')") is True  # no sale in December is 0


def test_flag_reads_yes_no_cells(con):
    assert run_select(con, "select flag('x'), flag('Có'), flag('1'), flag(''), flag(null), flag(false)")[1][0] == (
        True, True, True, False, False, False)


def test_shift_hours_are_session_parameters(tmp_path):
    tables, roles = shop()
    path = tmp_path / "s.duckdb"
    build_store(path, tables, roles)
    c = connect(path, "2025-02-28", {"shift_morning_from": 7, "shift_morning_to": 11})
    assert run_select(c, "select shift_of(timestamp '2025-01-01 06:30'), shift_of(timestamp '2025-01-01 07:30'), "
                         "shift_of(timestamp '2025-01-01 14:00')")[1][0] == (None, "sáng", "chiều")
    c.close()  # one database instance per file and process: its settings are locked by the first session
    c = connect(path, "2025-02-28", {"shift_morning_from": None, "shift_morning_to": None})
    assert one(c, "select shift_of(timestamp '2025-01-01 07:30')") is None  # unreadable rule: no shift, no guess


def test_rewrites_keep_macro_calls_runnable():
    share = "share_customers_min_orders(6, date '2025-08-01', date '2025-08-31')"
    wrapped = f"-- type: percent\nselect round_half_up(100.0 * {share} / customers_min_orders(6, date '2025-08-01', date '2025-08-31'), 2)"
    assert rewrite_macros(wrapped).splitlines()[-1] == f"select round_half_up({share}, 2)"
    col = "-- type: boolean\nselect month_stats(date '2025-06-01', date '2025-06-30').revenue < month_stats(date '2025-07-01', date '2025-07-31').revenue"
    assert rewrite_macros(col).splitlines()[-1] == ("select (select revenue from month_stats(date '2025-06-01', date '2025-06-30')) < "
                                                    "(select revenue from month_stats(date '2025-07-01', date '2025-07-31'))")
    assert rewrite_macros("-- type: number\nselect hour_stats(date '2026-03-01', date '2026-03-31')").endswith(
        "select * from hour_stats(date '2026-03-01', date '2026-03-31')")
    assert "price_effect_at('Bạc xỉu (M)', 35000, 14)" in rewrite_macros("-- type: percent\nselect price_effect('Bạc xỉu (M)', 35000, 14)")
    indexed = "-- type: boolean\nselect month_stats(date '2026-01-01', date '2026-03-31').revenue[1] < 3"
    assert rewrite_macros(indexed) == indexed  # an index into a column: no safe rewrite, the repair message explains
    plain = "-- type: money\nselect revenue(date '2025-01-01', date '2025-01-31')"
    assert rewrite_macros(plain) == plain


def test_equality_on_a_hand_written_note_is_flagged():
    assert free_text_lint("select count(*) from raw_so where \"Ghi chú\" = 'bảo hành'")
    assert free_text_lint("select count(*) from raw_so where has_name(\"Ghi chú\", 'bảo hành')") is None
    assert free_text_lint("select count(*) from orders where \"Kênh\" = 'store'") is None


def test_unit_check_only_for_a_plain_count_in_a_base_unit():
    units = ("ly", "gói", "lọ", "phần")
    assert unit_asked("Tuần này bán được tổng cộng bao nhiêu gói?", units) == "gói"
    assert unit_asked("Hqua ban duoc bao nhieu lo", units) == "lọ"  # no diacritics: matched by folding
    assert unit_asked("hiện còn bn lô chưa bán hết", units) is None  # 'lô' is not 'lọ'
    assert unit_asked("Tỉ lệ huỷ mấy phần trăm?", units) is None
    d = "\"date\" between date '2026-01-01' and date '2026-01-31'"
    assert unit_shadow(f"select sum(qty_base) from sales_lines where counted and {d}", "bán mấy ly", units) == "ly"
    assert unit_shadow(f"select sum(qty_base) from sales_lines where counted and unit = 'ly' and {d}", "bán mấy ly", units) == "ly"
    assert unit_shadow(f"select sum(qty_base) from sales_lines where counted and same_text(\"Nhóm\", 'trà') and {d}",
                       "nhóm trà bán mấy ly", units) is None  # a group: not the rulebook's per-unit count
    assert unit_shadow(f"select sum(qty_base) from sales_lines where counted and product_name = 'Trà' and {d}", "bán mấy ly", units) is None


INDEX = ValueIndex({"Kênh bán": {"store": "store", "zalo oa": "zalo_oa"},
                    "Nhóm hàng": {"tra": "trà", "tra sua": "trà sữa"},
                    "Trả bằng": {"cash": "cash", "cod": "cod"}})


def test_value_hints_name_the_column_and_the_written_value():
    assert INDEX.hints("Doanh thu đơn bán tại quầy tháng này?") == ['- "tại quầy" -> "Kênh bán" = \'store\'']
    assert INDEX.hints("đơn trả bằng tiền mặt") == ['- "tiền mặt" -> "Trả bằng" = \'cash\'']  # 'trả' is not the group 'trà'
    hint = INDEX.hints("Tháng 9 nhóm trà bán được bao nhiêu?")
    assert hint[0].startswith('- "trà" -> "Nhóm hàng" = \'trà\'')
    assert "UNANSWERABLE" in INDEX.hints("Tháng 9 nhóm cà phê chiếm bao nhiêu %?")[0]
    assert INDEX.hints("Trà sữa trân châu bán mấy ly?", names=("Trà sữa trân châu",)) == []  # an item named in full


def test_subjective_superlative_unless_the_rulebook_defines_it():
    assert "UNANSWERABLE" in subjective_note("Khách trung thực nhất tháng 10/2025 là ai?", "")
    assert subjective_note("Khách trung thực nhất là ai?", "| Khách trung thực nhất | khách không huỷ đơn |") is None
    assert subjective_note("Lô Siro ăn ngon Ích Nhi gần hết hạn nhất?", "", names=("Siro ăn ngon Ích Nhi",)) is None
    assert subjective_note("Mặt hàng nào bán chạy nhất?", "") is None


def test_conversion_written_in_the_cell():
    t = pd.DataFrame({"Quy đổi": [1.0, 1.0, None], "Quy đổi__unit": ["thùng = 4 bịch", "thùng = 12 hộp", None]})
    units, factors = _written_rules(t, "Quy đổi")
    assert units.tolist()[:2] == ["thùng", "thùng"] and factors.tolist()[:2] == [4.0, 12.0]
    assert _written_rules(pd.DataFrame({"Quy đổi": [24.0, 12.0], "Quy đổi__unit": ["lon", "chai"]}), "Quy đổi") is None
