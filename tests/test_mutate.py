import random

import pandas as pd

from datamate.eval.mutate import UNSEEN, formats, name_spellings, recovered_roles, rename, run_rule_variants, shuffle, status_words, subtotal_rows
from datamate.ingest.business import assemble, extract_pieces
from datamate.ingest.parsers import parse_date, parse_number
from datamate.ingest.readme import Readme
from datamate.ingest.roles import normalize_status, role_for
from datamate.ingest.tables import RawSheet

COLS = ["Mã đơn", "Ngày bán", "Tên hàng", "SL", "Thành tiền", "Trạng thái"]
DATA = [["D1", "01/03/2025", "Bia", 2, 20000, "Hoàn thành"], ["D2", "02/03/2025", "Nước", 1, 5000, "Đã huỷ"]]


def pieces():
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *DATA]), False)
    return [(sh, pd.DataFrame(DATA, columns=COLS).astype(object))]


def readme():
    return Readme("", None)


def test_unseen_headers_are_unknown_to_the_rules():
    assert not [h for h in UNSEEN.values() if role_for(h, None)]


def test_shuffle_keeps_every_role():
    p, r, _ = shuffle(pieces(), readme(), random.Random(0))
    base, biz = assemble("x", "train", readme(), pieces(), []), assemble("x", "train", r, p, [])
    assert not [k for k in recovered_roles(base, biz, {}) if k[-1] in "!?"]


def test_rename_loses_roles_and_records_the_map():
    p, r, renamed = rename(pieces(), readme(), random.Random(0))
    assert renamed["Thành tiền"] == UNSEEN["line_amount"]
    biz = assemble("x", "train", r, p, [])
    assert recovered_roles(assemble("x", "train", readme(), pieces(), []), biz, renamed)["line_amount!"] == 1


def test_formats_change_the_text_not_the_value():
    (_, df), = formats(pieces(), readme(), random.Random(0))[0]
    assert df["Thành tiền"].iloc[0] == "20.000đ" and parse_number(df["Thành tiền"].iloc[0]) == 20000
    assert df["Ngày bán"].iloc[0] == "1 thg 3 2025" and parse_date(df["Ngày bán"].iloc[0], 2000, True).month == 3


def test_status_words_are_unknown_but_keep_the_status_apart():
    (_, df), = status_words(pieces(), readme(), random.Random(0))[0]
    assert df["Trạng thái"].tolist() == ["Chốt đơn xong", "Khách bom hàng"]
    assert normalize_status("Khách bom hàng") == "hoàn thành"  # what the rules alone do with it


def test_identical_rows_stay_identical_when_spellings_change():
    rows = [["D1", "01/03/2025", "Bia Sài Gòn", 2, 20000, "Xong"]] * 40
    sh = RawSheet("a.csv", None, pd.DataFrame(), False)
    (_, df), = name_spellings([(sh, pd.DataFrame(rows, columns=COLS).astype(object))], readme(), None)[0]
    assert df["Tên hàng"].nunique() == 1


def test_subtotal_rows_carry_the_block_total():
    rows = [[f"D{i}", "01/03/2025", "Bia", 1, 1000, "Xong"] for i in range(60)]
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *rows]), False)
    out, = subtotal_rows([sh], readme(), random.Random(0), every=25)
    extra = out.df[out.df[0].astype(str).str.startswith("Cộng") | out.df[1].astype(str).str.startswith("Cộng")]
    assert len(extra) == 2 and extra[4].tolist() == [25000.0, 25000.0]


def test_block_totals_do_not_reach_the_order():
    rows = [["D1", "01/03/2025", "Bia", 1, 1000, "Xong"], [None, None, "Nước", 1, 500, None], [None, None, "Trà", 1, 500, None],
            [None, None, "Ghi tay", None, 2000, None], ["D2", "02/03/2025", "Bia", 1, 1000, "Xong"]]
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *rows]), False)
    biz = assemble("x", "train", readme(), extract_pieces([sh], {"ma don"}, []), [])
    df = next(iter(biz.tables.values()))
    assert df["Thành tiền"].sum() == 3000


def test_a_miss_does_not_hide_the_totals_after_it():
    rows = [["D1", "01/03/2025", "Bia", 1, 1000, "Xong"], [None, None, "Ghi tay", None, 7, None]]
    rows += [[f"E{i}", "02/03/2025", "Bia", 1, 10, "Xong"] for i in range(3)]
    rows += [[None, None, "Ghi tay", None, 30, None]]
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *rows]), False)
    biz = assemble("x", "train", readme(), extract_pieces([sh], {"ma don"}, []), [])
    assert next(iter(biz.tables.values()))["Thành tiền"].sum() == 1037


def test_a_fee_equal_to_the_line_above_is_not_a_total():
    rows = [["D1", "01/03/2025", "Bia", 1, 30000, "Xong"], [None, None, "Phí ship", None, 30000, None]]
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *rows]), False)
    biz = assemble("x", "train", readme(), extract_pieces([sh], {"ma don"}, []), [])
    assert len(next(iter(biz.tables.values()))) == 2


def test_a_total_labelled_in_the_id_column_is_still_a_total():
    rows = [[f"D{i}", "01/03/2025", "Bia", 1, 1000, "Xong"] for i in range(3)] + [["Cộng lũy kế", None, None, None, 3000, None]]
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *rows]), False)
    biz = assemble("x", "train", readme(), extract_pieces([sh], {"ma don"}, []), [])
    assert next(iter(biz.tables.values()))["Thành tiền"].sum() == 3000


def test_voucher_lines_are_not_block_totals():
    rows = [["D1", "01/03/2025", "Bia", 1, 1000, "Xong"], [None, None, "Voucher", None, -100, None]]
    sh = RawSheet("a.csv", None, pd.DataFrame([COLS, *rows]), False)
    biz = assemble("x", "train", readme(), extract_pieces([sh], {"ma don"}, []), [])
    assert len(next(iter(biz.tables.values()))) == 2


def test_rule_variants_report(tmp_path):
    rulebook = ("| Đơn bị huỷ | đơn có trạng thái huỷ. Đơn trả hàng **không** phải đơn huỷ |\n"
                "| Đơn tính vào doanh thu | x |\n| Đơn thuộc kỳ nào | y |\n")
    rep = run_rule_variants(rulebook, tmp_path, lambda system, parts: '{"changed": true}')
    assert rep["caught"][0] == rep["caught"][1]
