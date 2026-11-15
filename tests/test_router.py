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
    # pick_rules prefixes the section; the delta holds bare lines (public run: 0/1000 questions matched before)
    assert route_reasons("x", ["[Sửa xe] | A | b |"], frozenset(), frozenset({"| A | b |"}), False, 0.95, cfg) == [
        "changed rule"]


def test_route_reasons_sees_pick_rules_lines():
    from datamate.agent.evidence import pick_rules, rule_units

    book = "# Sửa xe\n| Thuật ngữ | Định nghĩa |\n|---|---|\n| Phiếu bảo hành | phiếu sửa lại miễn phí trong 30 ngày |\n"
    delta = frozenset(line for _, _, line in rule_units(book))
    rules = pick_rules(book, "tháng 5 có bao nhiêu phiếu bảo hành")
    assert rules and route_reasons("x", rules, frozenset(), delta, False, 0.95, RunConfig(think_route=True)) == ["changed rule"]


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


def test_cut_thinking_retries_without_thinking():
    from datamate.agent.llm_local import Reply
    from datamate.agent.pipeline import _samples
    from datamate.postprocess import Expect

    class FakeLLM:
        def __init__(self, thinking):
            self.enable_thinking, self.calls = thinking, []

        def without_thinking(self):
            self.plain = FakeLLM(False)
            return self.plain

        def generate(self, system, parts, history, temperature, seed, max_tokens=None):
            self.calls.append(history)
            r = Reply("" if self.enable_thinking else "```sql\nselect 7 as answer\n```")
            r.finish_reason = "length" if self.enable_thinking else "stop"
            return r

    class Pool:
        def run(self, code, as_of, shadow, **kw):
            return {"result": {"type": "number", "answer": 7}, "error": None, "stdout": ""}

    llm = FakeLLM(True)
    s = _samples(llm, {"as_of_date": "2025-01-31"}, Pool(), "sys", ["q"], "q", Expect(), None, {}, frozenset(), [], False,
                 RunConfig(engine="sql"))
    assert s[0]["result"] == {"type": "number", "answer": 7}
    assert [a["finish_reason"] for a in s[0]["attempts"]] == ["length", "stop"]
    assert llm.plain.calls == [[]]  # the empty cut reply is not sent back as a turn


def test_cut_repair_round_gets_one_more_try_without_thinking():
    """Train route run: 8 abstains whose first SQL failed and whose repair thinking was cut empty."""
    from datamate.agent.llm_local import Reply
    from datamate.agent.pipeline import _samples
    from datamate.postprocess import Expect

    class FakeLLM:
        def __init__(self, thinking, script):
            self.enable_thinking, self.script = thinking, script

        def without_thinking(self):
            return FakeLLM(False, self.script)

        def generate(self, system, parts, history, temperature, seed, max_tokens=None):
            text, finish = self.script.pop(0)
            r = Reply(text)
            r.finish_reason = finish
            return r

    class Pool:
        def run(self, code, as_of, shadow, **kw):
            if "bad" in code:
                return {"result": None, "error": "Binder Error: no column bad", "stdout": ""}
            return {"result": {"type": "number", "answer": 7}, "error": None, "stdout": ""}

    script = [("```sql\nselect bad\n```", "stop"), ("", "length"), ("```sql\nselect 7 as answer\n```", "stop")]
    s = _samples(FakeLLM(True, script), {"as_of_date": "2025-01-31"}, Pool(), "sys", ["q"], "q", Expect(), None, {},
                 frozenset(), [], False, RunConfig(engine="sql"))
    assert [a["finish_reason"] for a in s[0]["attempts"]] == ["stop", "length", "stop"]
    assert s[0]["result"] == {"type": "number", "answer": 7}


def test_reply_after_a_cut_keeps_its_repair_round():
    """public_e: 31 of 43 failed questions had their thinking cut first, then one try without thinking and no repair."""
    from datamate.agent.llm_local import Reply
    from datamate.agent.pipeline import _samples
    from datamate.postprocess import Expect

    class FakeLLM:
        def __init__(self, thinking, script):
            self.enable_thinking, self.script = thinking, script

        def without_thinking(self):
            return FakeLLM(False, self.script)

        def generate(self, system, parts, history, temperature, seed, max_tokens=None):
            text, finish = self.script.pop(0)
            r = Reply(text)
            r.finish_reason = finish
            return r

    class Pool:
        def run(self, code, as_of, shadow, **kw):
            if "bad" in code:
                return {"result": None, "error": "Binder Error: no column bad", "stdout": ""}
            return {"result": {"type": "number", "answer": 7}, "error": None, "stdout": ""}

    script = [("", "length"), ("```sql\nselect bad\n```", "stop"), ("```sql\nselect 7 as answer\n```", "stop")]
    s = _samples(FakeLLM(True, script), {"as_of_date": "2025-01-31"}, Pool(), "sys", ["q"], "q", Expect(), None, {},
                 frozenset(), [], False, RunConfig(engine="sql"))
    assert [a["finish_reason"] for a in s[0]["attempts"]] == ["length", "stop", "stop"]
    assert s[0]["result"] == {"type": "number", "answer": 7}


def test_think_retries_a_cut_compile_reply_without_thinking():
    from datamate.agent.llm_local import Reply
    from datamate.agent.pipeline import _think

    class FakeLLM:
        def __init__(self, thinking):
            self.enable_thinking = thinking

        def without_thinking(self):
            return FakeLLM(False)

        def generate(self, system, parts, history, temperature, seed, max_tokens=None):
            r = Reply("" if self.enable_thinking else "```sql\ncreate macro m() as 1;\n```")
            r.finish_reason, r.reasoning = ("length", "x" * 9) if self.enable_thinking else ("stop", None)
            return r

    reply = _think(FakeLLM(True), RunConfig(), "sys", ["section"], [])
    assert reply.startswith("```sql") and reply.cut.finish_reason == "length"
    assert not hasattr(_think(FakeLLM(False), RunConfig(), "sys", ["s"], []), "cut")


def test_shop_gate_abstains_a_shop_unlike_train(tmp_path):
    import json

    from datamate.agent.calibrate import apply_shop_gate, fit_shop_gate

    def run(d, answers):  # answers: {biz: [final, ...]}
        (d / "logs").mkdir(parents=True)
        lines = []
        for b, finals in answers.items():
            for i, f in enumerate(finals):
                qid = f"{b}_{i}"
                (d / "logs" / f"{qid}.json").write_text(json.dumps({"id": qid, "business_id": b, "final": f}))
                lines.append(json.dumps({"id": qid, **f}))
        (d / "submission.jsonl").write_text("\n".join(lines) + "\n")
        return d

    num, abst = {"type": "number", "answer": 1}, {"type": "abstain"}
    gate = fit_shop_gate([run(tmp_path / "train", {"a": [num] * 19 + [abst], "b": [num] * 20})], tmp_path / "g.json")
    assert gate["max"]["abstain"] == 0.05
    test = run(tmp_path / "test", {"c": [num] * 19 + [abst], "d": [num] * 17 + [abst] * 3})
    assert apply_shop_gate(test, gate) == ["d"]
    sub = [json.loads(x) for x in (test / "submission.jsonl").read_text().splitlines()]
    assert all(p["type"] == "abstain" for p in sub if p["id"].startswith("d_"))
    assert sum(p["type"] == "number" for p in sub if p["id"].startswith("c_")) == 19
    assert apply_shop_gate(test, gate) == ["d"]  # rerun reads the ungated file, not its own output
