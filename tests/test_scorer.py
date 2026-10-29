from datamate.eval.scorer import is_correct, score_one


def test_string_alias_and_normalization():
    ref = {"type": "string", "answer": "Sting dâu 330ml", "alias": ["SP0042"]}
    assert is_correct({"type": "string", "answer": "sting  DÂU-330ml"}, ref)
    assert is_correct({"type": "string", "answer": "sp0042"}, ref)
    assert not is_correct({"type": "string", "answer": "Sting dau 330ml"}, ref)  # diacritics matter


def test_number_percent_bool():
    assert is_correct({"type": "number", "answer": 48250000}, {"type": "number", "answer": 48250000})
    assert not is_correct({"type": "number", "answer": 48250001}, {"type": "number", "answer": 48250000})
    assert is_correct({"type": "percent", "answer": -12.449999}, {"type": "percent", "answer": -12.45})
    assert not is_correct({"type": "boolean", "answer": 1}, {"type": "boolean", "answer": True})
    assert not is_correct({"type": "number", "answer": 12.45}, {"type": "percent", "answer": 12.45})


def test_list_with_per_position_alias():
    ref = {"type": "list", "answer": ["Cà phê sữa (M)", "Bạc xỉu"], "alias": [["SP0003"], ["SP0017"]]}
    assert is_correct({"type": "list", "answer": ["SP0003", "bạc xỉu"]}, ref)
    assert not is_correct({"type": "list", "answer": ["Bạc xỉu", "Cà phê sữa (M)"]}, ref)


def test_table_rows_as_set_and_columns_by_name():
    ref = {"type": "table", "answer": {"columns": ["khach_hang", "so_tien_no"],
                                       "rows": [["Chị Lan", 3200000], ["Anh Tú", 1500000]]}, "alias": [["KH01"], ["KH02"]]}
    pred = {"type": "table", "answer": {"columns": ["so_tien_no", "khach_hang"],
                                        "rows": [[1500000, "Anh Tú"], [3200000, "kh01"]]}}
    assert is_correct(pred, ref)


def test_scores():
    ref = {"type": "unanswerable", "answer": None}
    assert score_one({"type": "unanswerable"}, ref) == 1
    assert score_one({"type": "abstain"}, ref) == 0
    assert score_one(None, ref) == 0
    assert score_one({"type": "number", "answer": 0}, ref) == -1
