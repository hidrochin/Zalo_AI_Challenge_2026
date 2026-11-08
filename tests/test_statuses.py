import json

import pandas as pd
import pytest

from datamate.agent.statuses import parse_labels, resolve_statuses
from datamate.canon import build_sales
from datamate.ingest.business import Business, TableInfo, _clean, apply_status, load_business, status_path
from datamate.ingest.readme import Readme
from datamate.ingest.roles import is_known_status


@pytest.mark.parametrize("value", ["Đã hoàn thành", "xong", "OK", "Đã lấy", "Đang xử lý", "Khách huỷ", "Hoàn trả", "Đang giao dịch"])
def test_known_statuses(value):
    assert is_known_status(value)


@pytest.mark.parametrize("value", ["Không nhận hàng", "Bom hàng", "Chờ xác nhận", "Hoàn tất một phần", "Hoàn tiền"])
def test_unknown_statuses_are_not_taken_for_finished(value):
    assert not is_known_status(value)


def clean(columns: dict, roles: dict) -> TableInfo:
    ti = TableInfo("t", [("f", None)], {}, roles)
    _clean(pd.DataFrame({**columns, "_file": "f", "_sheet": None}), ti)
    return ti


def test_only_order_tables_report_unknown_statuses():
    orders = clean({"ma": ["A", "B"], "tt": ["xong", "Bom hàng"]}, {"ma": "order_id", "tt": "status"})
    devices = clean({"may": ["X"], "tinh trang": ["Pin yếu"]}, {"tinh trang": "status"})
    assert orders.odd_status == ["Bom hàng"] and devices.odd_status == []


def test_parse_keeps_valid_labels_of_asked_values():
    reply = 'Đây:\n{"Bom hàng": "huỷ", "Chờ xác nhận": null, "Khác": "huỷ", "Hoàn tiền": "bị hoàn"}'
    assert parse_labels(reply, ["Bom hàng", "Chờ xác nhận", "Hoàn tiền"]) == {"bom hang": "huỷ", "cho xac nhan": None, "hoan tien": None}
    assert parse_labels("không biết", ["Bom hàng"]) == {"bom hang": None}


def shop() -> Business:
    df = pd.DataFrame({"ma": ["A", "B"], "ngay": pd.to_datetime(["2025-01-01"] * 2), "tien": [100, 50], "sl": [1, 1],
                       "ten": ["Gạo", "Muối"], "tt": ["xong", "Bom hàng"], "tt__norm": ["hoàn thành", "hoàn thành"]})
    roles = {"ma": "order_id", "ngay": "order_date", "tien": "line_amount", "sl": "qty", "ten": "product_name", "tt": "status"}
    ti = TableInfo("t", [("f", None)], {}, roles, odd_status=["Bom hàng"])
    return Business("x", "train", Readme("", None), {"t": df}, {"t": ti}, [])


def test_a_label_changes_what_counts():
    biz = shop()
    apply_status(biz, {"bom hang": "huỷ"})
    sales, _ = build_sales(biz.tables, {"t": biz.info["t"].roles})
    assert sales["counted"].tolist() == [True, False] and not biz.warnings


def test_a_value_left_unsure_stays_counted_with_a_warning():
    biz = shop()
    apply_status(biz, {"bom hang": None})
    assert biz.tables["t"]["tt__norm"].tolist() == ["hoàn thành", "hoàn thành"]
    assert "Bom hàng" in biz.warnings[0]


def test_resolve_asks_once_and_saves(tmp_path):
    calls = []

    def generate(system, parts):
        calls.append(parts)
        return '{"Bom hàng": "huỷ"}'

    biz = shop()
    status_path(tmp_path, "train", "x").parent.mkdir(parents=True)
    assert resolve_statuses(biz, tmp_path, generate) is True
    assert resolve_statuses(biz, tmp_path, generate) is False
    assert len(calls) == 1 and "Bom hàng" in calls[0][1]
    assert json.loads(status_path(tmp_path, "train", "x").read_text(encoding="utf-8")) == {"bom hang": "huỷ"}


def test_shop_with_known_statuses_makes_no_call(tmp_path):
    biz = shop()
    biz.info["t"].odd_status = []
    assert resolve_statuses(biz, tmp_path, lambda *_: pytest.fail("no call expected")) is False


def test_load_applies_saved_labels(tmp_path):
    split = tmp_path / "train"
    bdir = split / "data" / "x"
    bdir.mkdir(parents=True)
    pd.DataFrame({"Mã đơn": ["A", "B"], "Trạng thái": ["xong", "Bom hàng"]}).to_csv(bdir / "don.csv", index=False)
    cache = tmp_path / "cache"
    biz = load_business(split, "x", cache)
    name = next(iter(biz.tables))
    assert biz.info[name].odd_status == ["Bom hàng"]
    status_path(cache, "train", "x").write_text('{"bom hang": "huỷ"}', encoding="utf-8")
    assert load_business(split, "x", cache).tables[name]["Trạng thái__norm"].tolist() == ["hoàn thành", "huỷ"]
