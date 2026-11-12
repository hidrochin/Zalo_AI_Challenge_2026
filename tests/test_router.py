"""Rulebook delta compile, the thinking router and the calibrated decision."""

import json
from types import SimpleNamespace

import duckdb

from datamate.agent.calibrate import calibrate, features, p_correct
from datamate.agent.calibrate import tested_names as rule_macros
from datamate.agent.llm_local import reply_of
from datamate.agent.pipeline import RunConfig, escalate_reasons, route_reasons
from datamate.rules import TRAIN_RULEBOOK, rule_delta
from datamate.sql.compile import compile_delta

TRAIN = TRAIN_RULEBOOK.read_text(encoding="utf-8")


def boom(system, parts):
    raise AssertionError("no model call expected")


def test_train_rulebook_has_no_delta(tmp_path):
    assert rule_delta(TRAIN, TRAIN, tmp_path / "d.json", boom) == []


def test_new_rule_and_changed_rule(tmp_path):
    book = TRAIN.replace("| Đơn thuộc kỳ nào | theo **ngày đặt**", "| Đơn thuộc kỳ nào | theo **ngày giao**") + (
        "\n### Tiệm sửa xe\n| Lượt sửa | mỗi phiếu sửa là một lượt |\n")
    asked = []

    def gen(system, parts):
        asked.append(parts[0])
        return '{"changed": true}'

    delta = rule_delta(book, TRAIN, tmp_path / "d.json", gen)
    assert [(s, t) for s, t, _ in delta] == [("1. Đơn hàng và doanh thu", "Đơn thuộc kỳ nào"), ("Tiệm sửa xe", "Lượt sửa")]
    assert asked == ["Rule: Đơn thuộc kỳ nào"]  # a new term needs no verdict
    assert rule_delta(book, TRAIN, tmp_path / "d.json", boom) == delta  # verdicts cached


def test_changed_wording_the_model_reads_as_same_is_not_delta(tmp_path):
    book = TRAIN.replace("| Đơn thuộc kỳ nào | theo **ngày đặt**", "| Đơn thuộc kỳ nào | tính theo **ngày đặt đơn**")
    assert rule_delta(book, TRAIN, tmp_path / "d.json", lambda s, p: '{"changed": false}') == []


def test_compile_delta_refuses_a_tested_name(tmp_path):
    db = tmp_path / "s.duckdb"
    con = duckdb.connect(str(db))
    con.execute('create table orders as select date \'2025-01-02\' as "date", 1 as order_id')
    con.close()
    replies = iter([
        "```sql\n-- doc: x\n-- args: a date, b date\ncreate macro revenue(a, b) as (select count(*) from orders)\n```",
        "```sql\n-- doc: x\n-- args: a date, b date\ncreate macro trips_new(a, b) as (select count(*) from orders "
        'where "date" between a and b)\n```',
    ])
    got = compile_delta(TRAIN, [("Sửa xe", "Lượt sửa", "| Lượt sửa | mỗi phiếu |")], {"revenue": "revenue(a, b)"},
                        frozenset(), {"s": db}, lambda system, parts, history: next(replies), tmp_path / "m.json",
                        log=lambda _: None)
    assert [m.name for m in got] == ["trips_new"]
    assert compile_delta(TRAIN, [], {}, frozenset(), {}, None, tmp_path / "m.json") == []


def test_route_reasons():
    cfg = RunConfig(think_route=True)
    assert route_reasons("doanh thu tháng 7", [], frozenset(), frozenset(), False, 0.95, cfg) == []
    assert route_reasons("x", ["| A | b |"], frozenset(), frozenset({"| A | b |"}), True, 0.5, cfg) == [
        "changed rule", "suspect sales", "unfamiliar (0.50)"]


def test_escalate_reasons():
    ok = [{"attempts": [{"finish_reason": "stop"}]}]
    assert escalate_reasons({"type": "number", "answer": 1}, ok, None) == []
    assert escalate_reasons({"type": "unanswerable"}, ok, None) == ["unanswerable"]
    warned = [{"attempts": [{"warning": "x", "finish_reason": "length"}]}]
    assert escalate_reasons({"type": "number", "answer": 1}, warned, "hmm") == ["hedge", "check", "length"]


def test_reply_keeps_reasoning_and_finish_reason():
    msg = SimpleNamespace(content="```sql\nselect 1\n```", reasoning_content=None, model_extra={"reasoning": "nghĩ"})
    r = reply_of(SimpleNamespace(message=msg, finish_reason="length"))
    assert r == msg.content and r.reasoning == "nghĩ" and r.finish_reason == "length"


def _log(tmp, i, biz, tier, answer):
    rec = {"id": f"q{i}", "business_id": biz, "final": {"type": "number", "answer": answer}, "near": 0.9,
           "route": {"tier": tier, "reasons": []}, "samples": [{"result": {"type": "number", "answer": answer},
                                                                "attempts": [{"code": "select revenue(a, b)"}]}]}
    (tmp / "logs" / f"q{i}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_calibrate_learns_a_wrong_tier(tmp_path):
    run, split = tmp_path / "run", tmp_path / "train"
    (run / "logs").mkdir(parents=True)
    split.mkdir()
    refs = []
    for i in range(80):
        biz, deep = f"b{i % 4}", i % 2 == 0
        _log(run, i, biz, "escalated" if deep else "normal", 1)
        refs.append({"id": f"q{i}", "type": "number", "answer": 2 if deep and i % 10 else 1})  # escalated: mostly wrong
    (split / "answers.jsonl").write_text("\n".join(json.dumps(r) for r in refs), encoding="utf-8")
    rep = calibrate([run], split, tmp_path / "cal.json")
    assert rep["sum_after_lobo"] > rep["sum_before"]
    model = json.loads((tmp_path / "cal.json").read_text(encoding="utf-8"))
    rec = json.loads((run / "logs" / "q0.json").read_text(encoding="utf-8"))
    assert p_correct(model, features(rec, rule_macros())) < 0.5
    rec["route"]["tier"] = "normal"
    assert p_correct(model, features(rec, rule_macros())) > 0.5
