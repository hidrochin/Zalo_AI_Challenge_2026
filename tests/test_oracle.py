"""The oracle experiment's plumbing (debug/oracle): temp views set up on every cursor of the SQL pool."""

from datamate.sql.agent import SqlPool
from datamate.sql.store import build_store

from test_rule_macros import shop


def test_setup_reaches_every_cursor(tmp_path):
    tables, roles = shop()
    path = tmp_path / "shop.duckdb"
    build_store(path, tables, roles)
    pool = SqlPool(path, None, n=3)
    pool.setup(["create or replace temp view oracle_orders_seen as select count(*) as n from orders"])
    sql = "-- type: count\nselect n from oracle_orders_seen"
    assert [pool.run(sql, "2025-02-28")["result"]["answer"] for _ in range(6)] == [5] * 6
    pool.close()
