from pathlib import Path

import pytest

from datamate.eval.semantic import is_gated
from datamate.rules import DEPENDS, TESTED, parse_clauses, rule_gate, rule_note, unchanged

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


@pytest.mark.parametrize("file", ["data/train/rulebook.md", "data/public_test/rulebook.md"])
def test_shipped_rulebooks_keep_every_definition(file, tmp_path):
    if not Path(file).exists():
        pytest.skip("no data")
    assert rule_gate(Path(file).read_text(encoding="utf-8"), tmp_path / "r.json", boom) == frozenset()


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
