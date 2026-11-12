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


def test_setup_binds_as_of_and_skips_a_failing_statement(tmp_path):
    """A compiled table macro on as_of() is bound at creation (Kaggle 2026-11-11: date_trunc(..., NULL) killed the run);
    it then reads the question's as_of. A broken statement is skipped, not raised."""
    tables, roles = shop()
    path = tmp_path / "shop.duckdb"
    build_store(path, tables, roles)
    pool = SqlPool(path, None, n=2)
    pool.setup(["create temp macro month_start() as table select date_trunc('month', as_of())::date as d",
                "create temp view broken as select * from no_such_table"])
    sql = "-- type: date\nselect d from month_start()"
    assert [pool.run(sql, "2025-02-20")["result"]["answer"] for _ in range(4)] == ["2025-02-01"] * 4
    pool.close()
