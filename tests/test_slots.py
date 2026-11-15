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


def unit_shop(base_qty: bool = False) -> Business:
    """Three products sold by the bag (300 each) and, one line in five, by the case of 4 bags (1200), with no conversion
    table: the case lines keep qty_base = qty."""
    n = 60
    case = [i % 5 == 0 for i in range(n)]
    df = lines(n, hang=[f"Bỉm {'ABC'[i % 3]}" for i in range(n)], dvt=["thùng" if c else "bịch" for c in case],
               sl=[1 if c else 2 for c in case], gia=[1200 if c else 300 for c in case], tien=[1200 if c else 600 for c in case])
    if base_qty:
        df["sl le"] = [4 if c else 2 for c in case]
    catalog = pd.DataFrame({"ten": ["Bỉm A", "Bỉm B", "Bỉm C"], "dv": ["bịch"] * 3, "ghi": ["thùng = 4 bịch"] * 3})
    return shop({"t": (df, {**ROLES, "dvt": "unit"}), "dm": (catalog, {"ten": "product_name", "dv": "base_unit"})})


def test_unit_factor_converts_the_case_lines_and_says_so():
    from datamate.audit import audit

    biz = unit_shop()
    roles = {n: ti.roles for n, ti in biz.info.items()}
    sales, desc = build_sales(biz.tables, roles)
    assert "WARNING: 12 goods lines sold in thùng" in desc and audit(biz, sales).flags.get("unit_gap")
    fill = {"base_qty_column": None, "factors": [{"product": f"Bỉm {x}", "unit": "thùng", "factor": 4} for x in "ABC"]}
    applied, rec = run_slots(biz, RULEBOOK, reply(fill), rebuild_of(biz))
    assert rec["unit_factor"]["result"] == "applied"
    sales, desc = build_sales(biz.tables, roles, applied)
    assert sales.loc[sales["unit"] == "thùng", "qty_base"].eq(4).all() and "WARNING" not in desc
    assert "unit_gap" not in audit(biz, sales).flags
    _, rec = run_slots(biz, RULEBOOK, reply({"factors": [{"product": f"Bỉm {x}", "unit": "thùng", "factor": 40} for x in "ABC"]}),
                       rebuild_of(biz))
    assert rec["unit_factor"]["result"].startswith("rejected")  # 30 a bag in a case against 300 a bag


def test_unit_factor_reads_a_column_already_in_base_units():
    biz = unit_shop(base_qty=True)
    applied, rec = run_slots(biz, RULEBOOK, reply({"base_qty_column": "sl le", "factors": []}), rebuild_of(biz))
    assert applied["unit_factor"] == {"unit_column": "unit", "base_qty_column": "sl le"}
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()}, applied)
    assert sales.loc[sales["unit"] == "thùng", "qty_base"].eq(4).all() and sales.loc[sales["unit"] == "bịch", "qty_base"].eq(2).all()


def test_unconverted_lines_of_a_product_with_converted_ones_are_no_gap():
    from datamate.canon import unit_gap

    s = pd.DataFrame({"unit": ["cái", "cái", "hộp"], "base_unit": ["hộp"] * 3, "qty": [1, 2, 1], "qty_base": [1, 2, 6],
                      "product_name": ["Bánh"] * 3, "is_goods": [True] * 3})
    assert not unit_gap(s)[1].any()  # train biz_106: the label is off, the quantities are right
    assert unit_gap(s.assign(qty_base=[1, 2, 1]))[1].tolist() == [True, True, False]


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
