import pandas as pd

def test_fact_roles_are_offered_only_for_missing_facts_and_checked_on_values():
    from datamate.agent.columns import FACT_ROLES, parse_roles

    df = pd.DataFrame({"X": ["a", "b", "c"], "Y": [1000, 2000, 3000]})
    allowed = FACT_ROLES["payments"]
    got = parse_roles('{"X": "pay_amount", "Y": "pay_amount"}', df, ["X", "Y"], set(), allowed)
    assert got == {"Y": "pay_amount"}  # text cannot be money
    assert parse_roles('{"Y": "order_total"}', df, ["Y"], set(), allowed) == {}  # not offered


def _shop(tables, roles):
    from datamate.ingest.business import Business, TableInfo

    info = {n: TableInfo(name=n, sources=[], descs={}, roles=r) for n, r in roles.items()}
    return Business(id="b", split="train", readme=None, tables=tables, info=info, warnings=[])


def test_a_cost_on_the_lines_is_not_an_order_total():
    from datamate.agent.columns import _fit_table

    lines = pd.DataFrame({"Ma": ["A", "A", "B"], "Tien": [100, 200, 300], "Von": [60, 90, 150], "Tong": [300, 300, 290]})
    biz = _shop({"L": lines}, {"L": {"Ma": "order_id", "Tien": "line_amount"}})
    sales = pd.DataFrame({"order_id": ["A", "A", "B"], "amount": [100, 200, 300]})
    assert _fit_table(biz, "L", {"Von": "order_total"}, sales) == {}
    assert _fit_table(biz, "L", {"Tong": "order_total"}, sales) == {"Tong": "order_total"}


def test_a_catalogue_holds_no_stock_card_and_no_purchases():
    from datamate.agent.columns import _fit_table

    cat = pd.DataFrame({"Ma": ["A", "B"], "Ban": [1, 0], "Tao": pd.to_datetime(["2025-01-01", "2025-01-02"]), "Gia": [1000, 2000], "SL": [3, 4]})
    biz = _shop({"C": cat}, {"C": {"Ma": "product_id"}})
    found = {"Ban": "stock_balance", "Tao": "stock_time", "Gia": "import_price", "SL": "import_qty"}
    assert _fit_table(biz, "C", found, None) == {}
    assert _fit_table(biz, "C", {"SL": "stock_balance"}, None) == {"SL": "stock_balance"}  # a stock report has this shape


def test_sale_lines_hold_no_expense_and_no_trip():
    from datamate.agent.columns import _fit_table

    lines = pd.DataFrame({"Ma": ["A", "A"], "Tien": [100, 200], "Giam": [0, 10], "STT": [1, 2]})
    biz = _shop({"L": lines}, {"L": {"Ma": "order_id", "Tien": "line_amount"}})
    assert _fit_table(biz, "L", {"Giam": "expense_amount", "STT": "trip"}, None) == {}
