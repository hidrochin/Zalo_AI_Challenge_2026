import pandas as pd

from datamate.ingest.business import _block_fill, _group_fill


def orders(n=40):
    """n orders of 3 rows; the customer and the order total only on the first row (rulebook §8)."""
    rows = []
    for i in range(n):
        for j in range(3):
            rows.append({"ma": f"D{i}", "khach": f"K{i}" if j == 0 else None, "tong": 300.0 if j == 0 else None,
                         "ten": f"P{j}" if j else None, "sl": 1})
    return pd.DataFrame(rows)


def test_first_row_only_order_columns_are_filled_but_line_columns_are_not():
    df = orders()
    filled, first_only = _group_fill(df, "ma", list(df.columns), skip=set(), line_cols=frozenset({"ten", "sl"}))
    assert filled == [] and first_only == ["khach", "tong"]
    assert df["khach"].notna().all() and (df.groupby("ma")["khach"].nunique() == 1).all()
    assert df["ten"].isna().sum() == 40  # a line column stays as written


def test_a_column_set_on_several_rows_keeps_the_consistency_rule():
    df = orders()
    df.loc[df.index % 3 == 1, "khach"] = df["khach"].ffill()  # two values per order, the same: the old rule fills it
    filled, first_only = _group_fill(df, "ma", list(df.columns), skip=set())
    assert "khach" in filled and "khach" not in first_only


def test_blocks_under_a_dated_row_fill_down_by_their_key():
    rows = []
    for i in range(30):
        for j in range(3):
            rows.append({"ngay": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i) if j == 0 else pd.NaT,
                         "khach": f"K{i}" if j == 0 else None, "no": 900.0 if j == 0 else None, "tra": 300.0,
                         "ghi chu": f"STT {i}"})
    df = pd.DataFrame(rows)
    key, filled = _block_fill(df, "ngay", list(df.columns))
    assert key == "ghi chu" and filled == ["ngay", "khach", "no"]
    assert df["ngay"].notna().all() and df["tra"].eq(300.0).all()


def test_blocks_without_a_key_are_left_alone():
    df = pd.DataFrame({"ngay": [pd.Timestamp("2025-01-01") if i % 3 == 0 else pd.NaT for i in range(90)],
                       "tra": [float(i) for i in range(90)]})  # every row its own value: nothing tells blocks apart
    assert _block_fill(df, "ngay", list(df.columns)) is None
    assert df["ngay"].isna().sum() == 60
