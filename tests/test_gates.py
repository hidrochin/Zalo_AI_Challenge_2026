from datamate.agent.gates import apply, gate


def log(final, rules, attempts=(), expect=None, judge=None):
    return {"final": final, "pre": {"rules": rules}, "expect": expect or {}, "judge": judge,
            "samples": [{"result": final if final["type"] != "abstain" else None, "attempts": list(attempts)}]}


def test_count_answered_with_money_size_abstains():
    assert gate(log({"type": "number", "answer": 2_500_000}, "thang 3 ban bao nhieu don"))[1] == "count_money"
    assert gate(log({"type": "number", "answer": 2_500_000}, "doanh thu thang 3 bao nhieu tien")) is None
    assert gate(log({"type": "number", "answer": 25}, "thang 3 ban bao nhieu don")) is None


def test_how_many_items_answered_by_list_becomes_its_length():
    assert gate(log({"type": "list", "answer": ["a", "b", "c"]}, "co bao nhieu mat hang het hang")) == (
        {"type": "number", "answer": 3}, "items_len")
    assert gate(log({"type": "list", "answer": ["a"]}, "liet ke ten bao nhieu mat hang")) is None


def test_table_without_table_words_abstains():
    assert gate(log({"type": "table", "answer": {"columns": ["a"], "rows": []}}, "ai mua nhieu nhat"))[1] == "table_words"
    assert gate(log({"type": "table", "answer": {"columns": ["a"], "rows": []}}, "top 3 khach kem so tien")) is None


def test_unanswerable_inside_sql_block_is_unanswerable():
    rec = log({"type": "abstain"}, "khach nam hay nu", [{"code": "UNANSWERABLE: no gender column", "error": "Parser"}])
    assert gate(rec)[1] == "unans_in_sql"
    rec["judge"] = {"action": "abstain"}
    assert gate(rec) is None


def test_length_cut_attempt_keeps_the_answer_and_apply_records_a_gate():
    rec = log({"type": "number", "answer": 5}, "x", [{"finish_reason": "length", "code": None}, {"finish_reason": "stop"}])
    assert gate(rec) is None
    rec = log({"type": "number", "answer": 2_500_000}, "thang 3 ban bao nhieu don")
    apply(rec)
    assert rec["final"] == {"type": "abstain"} and rec["gated"] == {"gate": "count_money", "from": {"type": "number", "answer": 2_500_000}}


def test_suspect_shop_keeps_only_bias_proof_types():
    for final in ({"type": "number", "answer": 5}, {"type": "percent", "answer": 1.5}, {"type": "list", "answer": ["a"]}):
        assert gate(log(final, "doanh thu"), "suspect")[1] == "shop_suspect"
    for final in ({"type": "date", "answer": "2025-01-02"}, {"type": "boolean", "answer": True},
                  {"type": "unanswerable", "reason": "x"}):
        assert gate(log(final, "ngay nao"), "suspect") is None
    assert gate(log({"type": "number", "answer": 5}, "doanh thu"), "clean") is None


def test_hard_expectation_mismatch_abstains():
    rec = log({"type": "number", "answer": 5}, "ngay nao ban nhieu nhat", expect={"type": "date", "hard": True})
    assert gate(rec)[1] == "expect_type"
    assert gate(log({"type": "unanswerable"}, "x", expect={"type": "date", "hard": True})) is None


def test_log_summary_and_deadline_skip():
    import time

    from datamate.agent.pipeline import RunConfig, log_summary, solve_question

    rec = {"id": "q1", "final": {"type": "number", "answer": 5}, "elapsed": 1.25, "judge": None,
           "samples": [{"attempts": [{"code": "select x", "error": "Binder", "result": None},
                                     {"code": "select 5", "error": None, "result": {"type": "number", "answer": 5}}]}]}
    assert log_summary(rec) == {"id": "q1", "answer": {"type": "number", "answer": 5}, "elapsed_ms": 1250,
                                "thinking": None, "code": ["select x", "select 5"],
                                "output": [{"error": "Binder"}, {"type": "number", "answer": 5}]}
    q = {"id": "q2", "business_id": "b", "question": "x", "as_of_date": "2025-01-01"}
    out = solve_question(q, None, None, "", "", None, RunConfig(deadline=time.time() - 1))
    assert out["final"] == {"type": "abstain"} and out["skipped"] == "deadline"
