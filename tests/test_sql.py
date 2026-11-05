import duckdb
import pandas as pd
import pytest

from datamate import dm
from datamate.canon import build_sales
from datamate.sql.dims import build_dim, entity_dims, entity_keys, name_form, name_keys, same_spelling, with_entity_keys
from datamate.sql.store import SqlRejected, build_store, connect, run_select, set_as_of


def test_name_form():
    assert name_form("Coca  Cola 330 ml") == name_form("COCA-COLA 330ML") == "coca cola 330ml"
    assert name_form("Bạc xỉu (M)") == "bạc xỉu m"


def test_spellings_merge_only_where_one_side_has_no_diacritics():
    assert same_spelling("thach ca phe", "thạch cà phê")
    assert same_spelling("bạc xỉu thach", "bac xiu thạch")
    assert not same_spelling("doãn dũng thanh", "đoàn dũng thanh")
    assert not same_spelling("lan anh", "lan")


def test_name_keys_merge_spellings_but_not_different_names():
    names = pd.Series(["Gạo thơm", "Gạo thơm", "GẠO  THƠM", "Gao thom", None, "Doãn Thanh", "Đoàn Thanh", "Doan Thanh"])
    keys = name_keys(names)
    assert keys[:4].nunique() == 1 and keys[0] == "gạo thơm"
    assert pd.isna(keys[4])
    assert keys[5] != keys[6]  # different letters: two people, even with a bare spelling that fits both
    assert keys[7] in (keys[5], keys[6])


def test_partly_marked_spellings_join_the_fullest_form():
    keys = name_keys(pd.Series(["bạc xỉu thach", "bac xiu thạch", "bạc xỉu thạch"]))
    assert keys.nunique() == 1 and keys[0] == "bạc xỉu thạch"


def test_entity_keys_prefer_codes():
    code = pd.Series(["SP1", "SP1", None, "KH1", "KH2"])
    name = pd.Series(["Coca 330ml", "COCA 330 ML", "Coca 330ml", "Lan", "Lan"])
    keys = entity_keys(code, name)
    assert keys.tolist()[:3] == ["SP1", "SP1", "SP1"]  # the code-less row takes its name's code
    assert keys[3] != keys[4]  # two codes with one name are two entities


def test_entity_keys_keep_name_when_it_maps_to_two_codes():
    keys = entity_keys(pd.Series(["K1", "K2", None]), pd.Series(["Lan", "Lan", "Lan"]))
    assert keys.tolist() == ["K1", "K2", "lan"]


def test_build_dim_lists_every_spelling():
    name = pd.Series(["Gạo thơm", "Gạo thơm", "GẠO THƠM"])
    dim = build_dim(name_keys(name), None, name)
    assert len(dim) == 1
    assert dim.loc[0, "display_name"] == "Gạo thơm"
    assert dim.loc[0, "aliases"] == ["Gạo thơm", "GẠO THƠM"]
    assert dim.loc[0, "n_lines"] == 3


def test_walkins_have_no_customer_key():
    sales = pd.DataFrame({"product_name": ["a", "a"], "customer_name": ["Lan", "Khách lẻ"], "walkin": [False, True]})
    out = with_entity_keys(sales)
    assert out["customer_key"].isna().tolist() == [False, True]
    assert out["customer_name"].tolist() == ["Lan", "Khách lẻ"]
    assert entity_dims(out)["dim_customer"]["entity_key"].tolist() == ["lan"]


def test_names_keep_one_spelling_and_the_source_one():
    sales = pd.DataFrame({"product_name": ["Gạo thơm", "Gạo thơm", "GẠO  THƠM", "Gao thom"]})
    out = with_entity_keys(sales)
    assert out["product_name"].nunique() == 1 and out["product_name"][0] == "Gạo thơm"
    assert out["product_name_raw"].tolist() == sales["product_name"].tolist()
    assert entity_dims(out)["dim_product"].loc[0, "n_lines"] == 4


def test_build_sales_unifies_names_and_customer_keys():
    lines = pd.DataFrame({"ma": ["A", "B", "C"], "ngay": pd.to_datetime(["2025-01-01"] * 3), "tien": [100, 50, 70], "sl": [1, 1, 1],
                          "ten": ["Coca 330ml", "COCA 330 ML", "Coca 330ml"], "khach": ["Lê Ngọc", "Le Ngoc", "Khách lẻ"]})
    roles = {"lines": {"ma": "order_id", "ngay": "order_date", "tien": "line_amount", "sl": "qty", "ten": "product_name",
                       "khach": "customer_name"}}
    s, _ = build_sales({"lines": lines}, roles)
    assert s["product_name"].nunique() == 1
    assert s["customer"].nunique() == 1 and s["customer"].isna().tolist() == [False, False, True]


def shop():
    lines = pd.DataFrame({
        "ma": ["A", "A", "B", "C", "D", "E"],
        "tien": [100, 50, 70, 30, 40, 1000],
        "sl": [1, 1, 1, 1, -1, 1],
        "ten": ["Gạo", "GẠO", "Gạo", "Gạo", "Gạo", "Gạo"],
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
    c = connect(path, "2025-01-31")
    yield c
    c.close()


def one(con, sql):
    return run_select(con, sql)[1][0][0]


def test_macros_apply_the_default_rules(con):
    a, b = "date '2025-01-01'", "date '2025-01-31'"
    assert one(con, f"select revenue({a}, {b})") == 180  # B cancelled, D has a returned line
    assert one(con, f"select order_count({a}, {b})") == 2
    assert one(con, f"select cancel_count({a}, {b})") == 1
    assert one(con, f"select cancel_rate({a}, {b})") == 25.0
    assert one(con, f"select avg_order_value({a}, {b})") == 90


def test_as_of_cuts_the_views_and_can_be_moved(con):
    a, b = "date '2025-01-01'", "date '2025-12-31'"
    assert one(con, f"select revenue({a}, {b})") == 180
    set_as_of(con, "2025-02-28")
    assert one(con, f"select revenue({a}, {b})") == 1180


def test_name_variants_group_into_one_row(con):
    assert one(con, "select count(*) from dim_product") == 1
    assert one(con, "select count(distinct product_key) from sales_lines") == 1


def test_round_half_up_matches_dm(con):
    for x, n in [(2.5, 0), (3.5, 0), (-2.5, 0), (1.005, 2), (0.125, 2), (12.345, 2), (99.995, 2), (1234567.5, 0)]:
        assert one(con, f"select round_half_up({x!r}, {n})") == dm.round_half_up(x, n)


def test_only_one_select_runs(con):
    for bad in ("drop table sales_all", "select 1; select 2", "create table z(a int)"):
        with pytest.raises(SqlRejected):
            run_select(con, bad)


def test_session_cannot_read_files_or_change_settings(con):
    with pytest.raises(duckdb.Error):
        run_select(con, "select * from read_csv('/etc/hosts')")
    with pytest.raises(duckdb.Error):
        con.execute("set enable_external_access = true")


def test_timeout_interrupts(con):
    with pytest.raises(duckdb.Error):
        run_select(con, "select count(*) from range(100000000000) a, range(1000) b", timeout=0.2)


def test_unknown_column_names_a_candidate(con):
    with pytest.raises(duckdb.BinderException, match="amount"):
        run_select(con, "select amout from sales_lines")


def test_shop_without_order_id_gets_sales_macros_only(tmp_path):
    lines = pd.DataFrame({"ngay": pd.to_datetime(["2025-01-01"]), "tien": [100], "sl": [1], "ten": ["Gạo"]})
    roles = {"lines": {"ngay": "order_date", "tien": "line_amount", "sl": "qty", "ten": "product_name"}}
    path = tmp_path / "flat.duckdb"
    build_store(path, {"lines": lines}, roles)
    c = connect(path, "2025-01-31")
    assert one(c, "select revenue(date '2025-01-01', date '2025-01-31')") == 100
    with pytest.raises(duckdb.Error):
        run_select(c, "select order_count(date '2025-01-01', date '2025-01-31')")
    c.close()
