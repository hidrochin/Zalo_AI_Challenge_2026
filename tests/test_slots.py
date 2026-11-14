import json

import pandas as pd

from datamate.canon import build_sales
from datamate.ingest.business import Business, TableInfo
from datamate.ingest.readme import Readme
from datamate.slots.apply import name_map
from datamate.slots.base import parse_json, run_slots

RULEBOOK = "| Trạng thái đơn | hoàn thành, huỷ, trả hàng, đang giao |"


def shop(tables: dict[str, tuple[pd.DataFrame, dict]]) -> Business:
    return Business("x", "train", Readme("", None), {n: t for n, (t, _) in tables.items()},
                    {n: TableInfo(n, [("f", None)], {}, r) for n, (_, r) in tables.items()}, [])


def rebuild_of(biz: Business):
    def rebuild(extra: dict) -> Business:
        out = shop({n: (biz.tables[n], {**biz.info[n].roles, **extra.get(n, {})}) for n in biz.tables})
        out.extra_roles = extra
        return out
    return rebuild


def reply(obj):
    return lambda system, parts: "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


def lines(n=60, **cols) -> pd.DataFrame:
    df = pd.DataFrame({"so": [f"D{i // 2}" for i in range(n)], "ngay": pd.Timestamp("2025-01-01") + pd.to_timedelta([i // 10 for i in range(n)], "D"),
                       "hang": ["Trà sữa"] * n, "sl": [2] * n, "gia": [100] * n, "tien": [200] * n})
    for k, v in cols.items():
        df[k] = v
    return df


ROLES = {"so": "order_id", "ngay": "order_date", "hang": "product_name", "sl": "qty", "gia": "unit_price", "tien": "line_amount"}


def test_status_in_a_note_column_cancels_the_whole_order():
    note = ["cancel", None, "Huỷ", None] + [None] * 56  # one line of each of two orders says so
    biz = shop({"t": (lines(ghi=note), ROLES)})
    applied, rec = run_slots(biz, RULEBOOK, reply({"status_column": None, "status_map": {"cancel": "huỷ", "Huỷ": "huỷ"}}),
                             rebuild_of(biz))
    assert rec["status_text"]["result"] == "applied" and applied["status_text"]["column"] == "ghi"  # column from the values
    sales, desc = build_sales(biz.tables, {"t": ROLES}, applied)
    assert not sales.loc[sales["order_id"].isin(["D0", "D1"]), "counted"].any()
    assert sales.loc[~sales["order_id"].isin(["D0", "D1"]), "counted"].all() and "order status read from `ghi`" in desc


def test_status_fill_that_cancels_most_orders_is_rejected():
    biz = shop({"t": (lines(ghi=["giao nhanh"] * 60), ROLES)})
    _, rec = run_slots(biz, RULEBOOK, reply({"status_column": "ghi", "status_map": {"giao nhanh": "huỷ"}}), rebuild_of(biz))
    assert rec["status_text"]["result"].startswith("rejected")


def test_no_gap_no_call():
    biz = shop({"t": (lines(), ROLES)})
    def boom(system, parts):
        raise AssertionError("no slot should ask")
    applied, rec = run_slots(biz, RULEBOOK, boom, rebuild_of(biz))
    assert applied == {} and {r["result"] for r in rec.values()} == {"no gap"}


def test_order_key_named_by_the_model_becomes_the_order_id():
    roles = {k: v for k, v in ROLES.items() if k != "so"}
    biz = shop({"t": (lines(stt=list(range(60))), roles)})  # `stt` is a row number: never offered
    applied, rec = run_slots(biz, RULEBOOK, reply({"order_column": "so"}), rebuild_of(biz), enabled=("order_key",))
    assert applied["order_key"] == {"table": "t", "column": "so"} and "'stt'" not in rec["order_key"]["trigger"]
    _, rec = run_slots(biz, RULEBOOK, reply({"order_column": "stt"}), rebuild_of(biz), enabled=("order_key",))
    assert rec["order_key"]["result"] == "no fill"


def test_short_names_read_as_the_full_names_of_the_list():
    sold = ["Cáp C", "Sạc 20W"] * 25 + ["Cáp Type-C 1m"] * 10
    short = pd.DataFrame({"Ghi tắt": ["Cáp C", "Sạc 20W"], "Tên hàng": ["Cáp Type-C 1m", "Củ sạc nhanh 20W Type-C"],
                          "Loại": ["phụ kiện", "phụ kiện"]})
    biz = shop({"t": (lines(hang=sold), ROLES), "ghi": (short, {"Tên hàng": "product_name"})})
    fill = {"table": [{"short_col": "Cáp C"}], "short_col": "Ghi tắt", "full_col": "Tên hàng"}  # table given wrong
    applied, rec = run_slots(biz, RULEBOOK, reply(fill), rebuild_of(biz))
    assert rec["name_map"]["result"] == "applied" and applied["name_map"]["table"] == "ghi"
    sales, _ = build_sales(biz.tables, {"t": ROLES, "ghi": {"Tên hàng": "product_name"}}, applied)
    assert set(sales["product_name"]) == {"Cáp Type-C 1m", "Củ sạc nhanh 20W Type-C"} and sales["Loại"].notna().all()


def test_a_short_name_of_two_products_is_dropped():
    t = pd.DataFrame({"s": ["Ốp", "Ốp", "CL"], "f": ["Ốp lưng A", "Ốp lưng B", "Kính cường lực"]})
    assert name_map(t, "s", "f") == {"cl": "Kính cường lực"}


def test_line_discount_left_in_the_amount_is_taken_off():
    biz = shop({"t": (lines(**{"Giảm giá": [10, 0] * 30}), ROLES)})  # differs within an order: a line discount
    applied, rec = run_slots(biz, RULEBOOK, reply({"discount_column": "Giảm giá", "kind": "amount", "level": "line"}),
                             rebuild_of(biz))
    assert rec["line_amount"]["result"] == "applied"
    sales, _ = build_sales(biz.tables, {"t": ROLES}, applied)
    assert sales["amount"].tolist()[:2] == [190, 200]
    _, rec = run_slots(biz, RULEBOOK, reply({"discount_column": "Giảm giá", "kind": "amount", "level": "order"}), rebuild_of(biz))
    assert rec["line_amount"]["result"] == "no fill"  # an order discount stays in revenue


def test_parse_json_reads_a_block_or_a_bare_object():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here: {"a": null} done') == {"a": None}
    assert parse_json("no idea") is None


def test_status_column_the_train_vocabulary_reads_in_full_when_the_model_names_none():
    note = ["cancelled", None, "returned", None] + [None] * 56
    biz = shop({"t": (lines(ghi=note), ROLES)})
    applied, rec = run_slots(biz, RULEBOOK, reply({"status_column": None, "status_map": {}}), rebuild_of(biz))
    assert applied["status_text"] == {"column": "ghi", "map": {"cancelled": "huỷ", "returned": "trả hàng"}}
    free = shop({"t": (lines(ghi=["khách quen", None, "huỷ", None] + [None] * 56), ROLES)})  # not every value a status
    _, rec = run_slots(free, RULEBOOK, reply({"status_column": None, "status_map": {}}), rebuild_of(free))
    assert rec["status_text"]["result"] == "no fill"
