from datamate.agent.vote import combine


def test_combine_answers_on_agreement_or_a_single_answer():
    a, b = {"type": "number", "answer": 12}, {"type": "number", "answer": 12.0}
    assert combine({"sql": a, "pandas": b}) == (a, {"answered": ["pandas", "sql"], "agree": True, "from": "sql"})
    assert combine({"sql": {"type": "abstain"}, "pandas": b})[0] == b
    assert combine({"sql": {"type": "unanswerable", "reason": "x"}, "pandas": {"type": "abstain"}})[0]["type"] == "unanswerable"


def test_combine_abstains_on_disagreement():
    assert combine({"sql": {"type": "number", "answer": 1}, "pandas": {"type": "number", "answer": 2}})[0] == {"type": "abstain"}
    assert combine({"sql": {"type": "unanswerable"}, "pandas": {"type": "number", "answer": 2}})[0] == {"type": "abstain"}
    assert combine({"sql": {"type": "abstain"}, "pandas": {"type": "abstain"}})[0] == {"type": "abstain"}


def test_tables_agree_on_rows_whatever_the_column_names():
    t1 = {"type": "table", "answer": {"columns": ["khach_hang", "so_tien_no"], "rows": [["A", 5], ["B", 3]]}}
    t2 = {"type": "table", "answer": {"columns": ["customer_name", "debt"], "rows": [["B", 3], ["A", 5]]}}
    assert combine({"sql": t1, "pandas": t2})[0] == t1
