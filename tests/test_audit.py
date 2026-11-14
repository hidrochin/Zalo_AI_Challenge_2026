import pandas as pd

from datamate.audit import audit, readme_roles
from datamate.ingest.business import Business, TableInfo
from datamate.ingest.readme import Readme


def biz(tables: dict[str, tuple[pd.DataFrame, dict, dict]]) -> Business:
    return Business("x", "train", Readme("", None), {n: t for n, (t, _, _) in tables.items()},
                    {n: TableInfo(n, [("f", None)], d, r) for n, (_, r, d) in tables.items()}, [])


def lines(n=60, note=None, discount=None, amount=None):
    df = pd.DataFrame({"ma": [f"D{i // 2}" for i in range(n)], "sl": [2] * n, "gia": [100] * n,
                       "tien": amount if amount is not None else [200] * n, "ghi chu": note or [None] * n})
    if discount is not None:
        df["Giảm giá"] = discount
    return df


ROLES = {"ma": "order_id", "sl": "qty", "gia": "unit_price", "tien": "line_amount"}


def sales_of(df: pd.DataFrame, status="hoàn thành") -> pd.DataFrame:
    out = df.rename(columns={"ma": "order_id", "sl": "qty", "gia": "unit_price", "tien": "amount"})
    return out.assign(status=status, is_goods=True)


def test_clean_shop():
    df = lines()
    assert audit(biz({"t": (df, ROLES, {})}), sales_of(df)).level == "clean"


def test_status_written_in_a_note_column_is_flagged():
    note = ["huỷ"] * 4 + [None] * 56  # whole-cell status phrases, and the canonical sales have no cancelled order
    df = lines(note=note)
    a = audit(biz({"t": (df, ROLES, {})}), sales_of(df))
    assert a.level == "suspect" and "status_in_text" in a.flags
    names = lines(note=["Huy lấy hộ"] * 4 + [None] * 56)  # a name in a note is no status
    assert audit(biz({"t": (names, ROLES, {})}), sales_of(names)).level == "clean"


def test_line_discount_left_in_the_amount_is_flagged_but_an_order_discount_is_not():
    per_line = lines(discount=[10, 0] * 30)  # differs within an order: a line discount, but amount = qty × price
    assert "discount_unapplied" in audit(biz({"t": (per_line, ROLES, {})}), sales_of(per_line)).flags
    per_order = lines(discount=[10] * 60)  # the same on every line of the order: the order's discount
    assert audit(biz({"t": (per_order, ROLES, {})}), sales_of(per_order)).level == "clean"


def test_order_level_cell_left_blank_on_later_rows_is_flagged():
    df = lines()
    df["khach"] = [f"K{i // 2}" if i % 2 == 0 else None for i in range(60)]  # only on the first row of each order
    assert "fill_gap" in audit(biz({"t": (df, ROLES, {})}), sales_of(df)).flags


def test_blocks_under_a_dated_row_are_flagged():
    df = pd.DataFrame({"ngay": [pd.Timestamp("2025-01-01") if i % 3 == 0 else pd.NaT for i in range(60)],
                       "tra": [100] * 60})
    assert "fill_gap" in audit(biz({"t": (df, {"ngay": "order_date"}, {})}), None).flags


def test_readme_sentence_of_a_train_column_without_a_role_is_a_role_gap():
    desc = "tiền hàng của đơn: cộng thành tiền các mặt hàng, trước giảm giá của đơn"
    train = biz({"o": (pd.DataFrame({"Tien hang": [1]}), {"Tien hang": "order_goods"}, {"Tien hang": desc})})
    known = readme_roles([train])
    df = lines()
    df["Giá trị"] = 200
    shop = biz({"t": (df, ROLES, {"Giá trị": desc})})
    a = audit(shop, sales_of(df), known)
    assert "role_gap" in a.flags and a.level == "suspect"
