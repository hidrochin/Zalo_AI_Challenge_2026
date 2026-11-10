from pathlib import Path

import pytest

from datamate.eval.semantic import is_gated
from datamate.rules import DEPENDS, TESTED, params_off, parse_clauses, rule_gate, rule_note, rule_params, unchanged

BOOK = "".join(f"| {k} | {v[0]} |\n" for k, v in TESTED.items())


def boom(system, parts):
    raise AssertionError("no model call expected")


def test_tested_text_needs_no_model(tmp_path):
    assert rule_gate(BOOK, tmp_path / "r.json", boom) == frozenset()


def test_every_dependency_is_tested():
    assert {k for needs in DEPENDS.values() for k in needs} == set(TESTED)


def test_formatting_is_not_a_change(tmp_path):
    book = BOOK.replace("**", "").replace("không huỷ", "Không  huỷ")
    assert rule_gate(book, tmp_path / "r.json", boom) == frozenset()


def test_train_rulebook_keeps_every_definition(tmp_path):
    file = Path("data/train/rulebook.md")
    if not file.exists():
        pytest.skip("no data")
    assert rule_gate(file.read_text(encoding="utf-8"), tmp_path / "r.json", boom) == frozenset()


def test_public_rulebook_drops_the_macros_of_its_removed_rules(tmp_path):
    file = Path("data/public_test/rulebook.md")
    if not file.exists():
        pytest.skip("no data")
    asked = []

    def gen(system, parts):
        asked.append(parts[0])
        return '{"changed": false}'

    off = rule_gate(file.read_text(encoding="utf-8"), tmp_path / "r.json", gen)
    assert off == {"price_on", "avg_import_price", "stock_split"}  # 'Giá bán X ngày D', 'Giá nhập bình quân…', 'Tồn theo đơn vị lớn'
    assert asked == ["Rule: Giao nhiều đợt"]  # reworded: asked once whether a number can differ


def test_rule_params_read_the_shift_hours():
    book = "| Ca sáng, ca chiều | theo giờ tạo đơn: ca sáng 06:00–13:59, ca chiều 14:00–21:59 |"
    assert rule_params(book) == {"shift_morning_from": 6, "shift_morning_to": 13, "shift_afternoon_from": 14, "shift_afternoon_to": 21}
    assert rule_params(book.replace("06:00–13:59", "7h–12h"))["shift_morning_to"] == 11  # ends before 12:00
    assert params_off(rule_params(book)) == frozenset()
    assert params_off(rule_params("| Ca sáng, ca chiều | buổi sáng và buổi chiều |")) == {"shift_revenue", "shift_orders"}


def test_changed_clause_turns_off_what_depends_on_it(tmp_path):
    book = BOOK.replace(TESTED["Tỉ lệ đơn huỷ trong kỳ"][0], "chỉ tính đơn đã xong")
    off = rule_gate(book, tmp_path / "r.json", lambda s, p: '{"changed": true}')
    assert off == {"cancel_rate"}


@pytest.mark.parametrize("reply", ['{"changed": null}', "không rõ", '{"changed": "no"}', ""])
def test_unsure_counts_as_changed(reply, tmp_path):
    book = BOOK.replace(TESTED["Tỉ lệ đơn huỷ trong kỳ"][0], "x")
    assert rule_gate(book, tmp_path / "r.json", lambda s, p: reply) == {"cancel_rate"}


def test_clear_no_keeps_the_definition_and_is_asked_once(tmp_path):
    book = BOOK.replace(TESTED["Tỉ lệ đơn huỷ trong kỳ"][0], "x")
    calls = []

    def gen(system, parts):
        calls.append(1)
        return '{"changed": false}'

    assert rule_gate(book, tmp_path / "r.json", gen) == frozenset()
    assert rule_gate(book, tmp_path / "r.json", gen) == frozenset() and len(calls) == 1


def test_missing_clause_turns_off(tmp_path):
    book = "\n".join(line for line in BOOK.splitlines() if not line.startswith("| Đơn bị huỷ"))
    assert rule_gate(book, tmp_path / "r.json", boom) == {"cancel_count", "cancel_rate"}


def test_unchanged_reads_only_a_clear_false():
    assert unchanged('x {"changed": false} y') and not unchanged('{"changed": true}') and not unchanged("[1]")


def test_clause_names_fold():
    assert "don bi huy" in parse_clauses(BOOK)


def test_note_names_what_is_off():
    assert rule_note(frozenset()) == ""
    assert "revenue" in rule_note(frozenset({"revenue"}))


def test_gated_question_loses_its_shadow_check():
    assert is_gated("doanh thu tháng 3/2025", frozenset({"revenue"}))
    assert not is_gated("doanh thu tháng 3/2025", frozenset({"cancel_rate"}))
