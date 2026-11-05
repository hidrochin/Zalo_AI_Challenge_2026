import json

import pandas as pd
import pytest

from datamate.agent.columns import parse_roles, propose_roles, resolve_columns, tables_to_map
from datamate.ingest.business import Business, TableInfo, load_business, roles_path
from datamate.ingest.readme import Readme
from datamate.ingest.trust import assess, role_problem


@pytest.mark.parametrize("values, role, bad", [
    (["a", "b"], "line_amount", True), ([1, 2, -3], "line_amount", False), ([-1, -2, 3], "line_amount", True),
    (["x", "y"], "order_date", True), (pd.to_datetime(["2025-01-01"]), "order_date", False), (["01/02/2025", "3/4/2025"], "order_date", False),
    (pd.to_datetime(["2025-01-01"]), "qty", True), ([str(i) for i in range(40)], "status", True), (["xong", "huỷ"], "status", False),
    ([1, 2], "customer_name", True), (["An", "Bình"], "customer_name", False),
])
def test_role_problem(values, role, bad):
    df = pd.DataFrame({"c": list(values)})
    assert bool(role_problem(df, "c", role)) is bad


def shop(line_roles=None, end=None, **cols):
    df = pd.DataFrame(cols or {"ngay": pd.to_datetime(["2025-01-01"]), "ten": ["Bia"], "sl": [2], "tien": [10]})
    roles = line_roles if line_roles is not None else {"ngay": "order_date", "ten": "product_name", "sl": "qty", "tien": "line_amount"}
    return Business("x", "train", Readme("", end), {"t": df}, {"t": TableInfo("t", [("f", None)], {}, roles)}, [])


def sales(n=40, price=100):
    return pd.DataFrame({"date": pd.to_datetime(["2025-01-01"] * n), "amount": [200] * n, "qty": [2] * n, "unit_price": [price] * n,
                         "order_id": ["D"] * n, "product_key": ["p"] * n, "qty_base": [2] * n})


def test_consistent_sales_are_trusted():
    t = assess(shop(), sales())
    assert t.states["revenue"] == "trusted" and t.states["customers"] == "absent" and not t.problems


def test_amounts_that_do_not_follow_quantity_and_price_are_suspect():
    t = assess(shop(), sales(price=7))
    assert t.states["revenue"] == "suspect" and "quantity × price" in t.note()


def test_sales_ending_far_from_the_readme_date_are_suspect():
    assert assess(shop(end=pd.Timestamp("2025-12-31")), sales()).states["revenue"] == "suspect"


def test_no_sales_is_absent_not_suspect():
    assert set(assess(shop(), None).states.values()) == {"absent"}


def test_a_shop_read_in_full_is_never_sent_to_the_model():
    roles = {"ngay": "order_date", "ten": "product_name", "sl": "qty", "tien": "line_amount", "tt": "status"}
    biz = shop(roles, ngay=pd.to_datetime(["2025-01-01"]), ten=["Bia"], sl=[2], tien=[10], tt=["xong"], ghi=["x"])
    assert tables_to_map(biz) == []


def test_a_shop_without_a_status_asks_about_its_order_tables():
    biz = shop(ngay=pd.to_datetime(["2025-01-01"]), ten=["Bia"], sl=[2], tien=[10], ghi=["huỷ"])
    assert tables_to_map(biz) == ["t"]


def test_a_shop_without_sales_asks_about_its_free_columns():
    biz = shop({}, ngay=pd.to_datetime(["2025-01-01"]), tien=[10])
    assert tables_to_map(biz) == ["t"]
    got = propose_roles(biz, lambda system, parts: '{"ngay": "order_date", "tien": "line_amount"}')
    assert got == {"t": {"ngay": "order_date", "tien": "line_amount"}}


def test_proposals_that_fail_on_the_values_or_repeat_a_role_are_dropped():
    df = pd.DataFrame({"a": ["x", "y"], "b": [1, 2], "c": [3, 4]})
    reply = 'Đây: {"a": "line_amount", "b": "qty", "c": "qty", "d": "qty"}'
    assert parse_roles(reply, df, ["a", "b", "c"], set()) == {"b": "qty"}
    assert parse_roles("không biết", df, ["a"], set()) == {}
    assert parse_roles('{"b": "qty"}', df, ["b"], {"qty"}) == {}
    assert parse_roles('{"b": "made_up"}', df, ["b"], set()) == {}


def test_resolve_saves_once(tmp_path):
    biz = shop({}, ngay=pd.to_datetime(["2025-01-01"]), tien=[10])
    calls = []

    def gen(system, parts):
        calls.append(parts)
        return '{"tien": "line_amount"}'

    (tmp_path / "train").mkdir()
    assert resolve_columns(biz, tmp_path, gen) is True
    assert resolve_columns(biz, tmp_path, gen) is False and len(calls) == 1
    assert json.loads(roles_path(tmp_path, "train", "x").read_text(encoding="utf-8")) == {"t": {"tien": "line_amount"}}


def test_saved_roles_reach_every_load(tmp_path):
    split = tmp_path / "train"
    (split / "data" / "s").mkdir(parents=True)
    (split / "data" / "s" / "b.csv").write_text("Doanh số,Khác\n10,a\n20,b\n", encoding="utf-8")
    cache = tmp_path / "cache"
    assert not load_business(split, "s", cache).info["b"].roles
    roles_path(cache, "train", "s").write_text('{"b": {"Doanh số": "line_amount"}}', encoding="utf-8")
    assert load_business(split, "s", cache).info["b"].roles == {"Doanh số": "line_amount"}


def test_codes_missing_from_the_product_list_are_suspect():
    biz = shop()
    biz.tables["sp"] = pd.DataFrame({"ma": ["A"], "ten": ["Bia"]})
    biz.info["sp"] = TableInfo("sp", [("g", None)], {}, {"ma": "product_id", "ten": "product_name"})
    assert assess(biz, sales().assign(product_id="A")).states["revenue"] == "trusted"
    assert assess(biz, sales().assign(product_id="Z")).states["revenue"] == "suspect"


def test_an_order_total_is_not_a_line_amount_when_product_lines_exist():
    biz = shop({"ten": "product_name", "tien": "line_amount"}, ten=["Bia"], tien=[10])
    biz.tables["don"] = pd.DataFrame({"ma": ["D"], "tong": [10]})
    biz.info["don"] = TableInfo("don", [("g", None)], {}, {})
    reply = {"don": '{"ma": "order_id", "tong": "line_amount"}', "t": "{}"}
    got = propose_roles(biz, lambda system, parts: reply["don" if "Table: don" in parts[0] else "t"])
    assert got == {"don": {"ma": "order_id"}}
