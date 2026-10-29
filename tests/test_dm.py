from pathlib import Path

import pandas as pd
import pytest

from datamate import dm
from datamate.agent.prompt import extract_code
from datamate.agent.vote import decide

CAL = Path("data/train/calendar.csv")


@pytest.fixture(autouse=True)
def ctx():
    cal = None
    if CAL.exists():
        cal = pd.read_csv(CAL)
        cal["date"] = pd.to_datetime(cal["date"])
    dm.set_context("2026-03-04", cal)  # a Wednesday


def test_periods():
    assert dm.week_prev() == (pd.Timestamp("2026-02-23"), pd.Timestamp("2026-03-01"))
    assert dm.week_this() == (pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-04"))
    assert dm.this_month().start == pd.Timestamp("2026-03-01")
    assert dm.prev_month_same_days() == (pd.Timestamp("2026-02-01"), pd.Timestamp("2026-02-04"))
    assert dm.month_auto(5).start.year == 2025
    assert dm.last_n_days(7).start == pd.Timestamp("2026-02-26")
    assert dm.quarter(2026, 2).end == pd.Timestamp("2026-06-30")


def test_prev_month_cap():
    dm.set_context("2026-03-31", None)
    assert dm.prev_month_same_days().end == pd.Timestamp("2026-02-28")


@pytest.mark.skipif(not CAL.exists(), reason="needs data/train/calendar.csv")
def test_tet_2026():
    t = dm.tet(2026)
    assert t.start == pd.Timestamp("2026-02-10")  # 23 tháng Chạp Ất Tỵ
    assert t.end == pd.Timestamp("2026-02-26")  # mùng 10 tháng Giêng Bính Ngọ
    assert dm.pre_tet(2026).end == pd.Timestamp("2026-02-09")


def test_in_period_clips_to_as_of():
    s = pd.Series(pd.to_datetime(["2026-03-01", "2026-03-05"]))
    assert dm.in_period(s, dm.month(2026, 3)).tolist() == [True, False]


def test_answers_and_rounding():
    assert dm.answer_number(10.5, kind="money")["answer"] == 11
    assert dm.answer_percent(-3.775)["answer"] == -3.78
    assert dm.answer_list([1.0, 2.5])["answer"] == [1, 2.5]


def test_rank_top_tie_by_code():
    df = pd.DataFrame({"code": ["B", "A", "C"], "qty": [5, 5, 1]})
    assert dm.rank_top(df, "qty", 2, code="code")["code"].tolist() == ["A", "B"]


def test_find_without_diacritics():
    assert dm.find(["Nước suối Lavie 1.5L", "Coca Cola 330ml"], "nuoc suoi lavie 1.5l")[0] == "Nước suối Lavie 1.5L"


def test_extract_code_and_vote():
    assert extract_code("plan\n```python\nx=1\nanswer_number(x)\n```") == "x=1\nanswer_number(x)\n"
    r = {"type": "number", "answer": 5}
    assert decide([r, r, None])[0] == r
    assert decide([r, {"type": "number", "answer": 6}, None])[0] == {"type": "abstain"}
