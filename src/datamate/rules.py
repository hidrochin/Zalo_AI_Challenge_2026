"""Rulebook clauses the fixed definitions depend on, so a changed rule cannot be overruled by old code.

The contracts, macros and `counted` encode the clauses in TESTED. Each load of a rulebook compares them: the same text
keeps a definition on; a different text asks the model once whether the meaning changed, and only a clear "no" keeps it
on; a missing clause turns it off. A definition that is off loses its shadow check, and the prompt says to follow the
rulebook instead of the helper.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

from datamate.text import fold

DEPENDS = {
    "revenue": ("Đơn tính vào doanh thu", "Doanh thu một đơn", "Đơn thuộc kỳ nào"),
    "order_count": ("Số đơn", "Đơn tính vào doanh thu", "Đơn thuộc kỳ nào"),
    "cancel_count": ("Đơn bị huỷ", "Đơn thuộc kỳ nào"),
    "cancel_rate": ("Tỉ lệ đơn huỷ trong kỳ", "Đơn bị huỷ", "Đơn thuộc kỳ nào"),
    "avg_order_value": ("Giá trị trung bình một đơn", "Đơn tính vào doanh thu", "Doanh thu một đơn", "Đơn thuộc kỳ nào"),
}
# Clause texts the definitions were checked against: the train rulebook (contracts on gold) and later wordings read by hand.
TESTED: dict[str, tuple[str, ...]] = {
    'Đơn tính vào doanh thu': (
        'đơn **không huỷ** và **không trả hàng**. Đơn có trả hàng, **kể cả chỉ trả một phần**, là đơn trả hàng: bỏ **cả đơn**. Đơn đang giao vẫn tính',
    ),
    'Doanh thu một đơn': (
        'tổng **thành tiền các dòng hàng** (đã trừ giảm giá trên từng dòng). **Không** trừ giảm giá trên cả đơn (khuyến mãi theo tổng tiền đơn, voucher shop), **không** cộng thuế VAT, **không** gồm phí giao hàng. **Tiền hàng**, **tiền bán** của đơn bán cùng nghĩa với doanh thu',
    ),
    'Đơn thuộc kỳ nào': (
        'theo **ngày đặt**, không theo ngày giao hay ngày thu tiền',
        'theo **ngày đặt**, không theo ngày giao hay ngày thu tiền. Đơn trả hàng sau kỳ, kể cả trả sau hôm nay, vẫn bị loại khỏi doanh thu kỳ đặt',
    ),
    'Số đơn': (
        'chỉ tính đơn không huỷ, không trả; cả khi đếm số đơn có một mặt hàng, số đơn của một khách. **Khách đã mua** trong kỳ là khách có ít nhất một đơn như vậy trong kỳ',
    ),
    'Đơn bị huỷ': (
        'đơn có trạng thái huỷ. Đơn trả hàng **không** phải đơn huỷ',
    ),
    'Tỉ lệ đơn huỷ trong kỳ': (
        'phần trăm đơn huỷ trong **mọi đơn đặt trong kỳ** (theo ngày đặt, **mọi trạng thái**: cả đơn huỷ, đơn trả, đơn đang giao). Làm tròn 2 chữ số thập phân.',
    ),
    'Giá trị trung bình một đơn': (
        'làm tròn về đồng, nửa đồng làm tròn lên',
    ),
}

SYSTEM = """A business rulebook was edited. You compare the old and new text of one rule. Answer whether the new text gives a different number for any order or question than the old text. Added detail that only spells out what the old text already implied is not a change. Reply with one JSON object: {"changed": false} when no number can differ, {"changed": true} when some number can differ, {"changed": null} when you are not sure. Nothing else."""

_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", re.M)
_JSON = re.compile(r"\{.*\}", re.S)


def parse_clauses(rulebook: str) -> dict[str, str]:
    """Folded clause name -> text, from the rulebook's two-column tables."""
    return {fold(m.group(1)): m.group(2) for m in _ROW.finditer(rulebook)}


def _plain(text: str) -> str:
    return " ".join(fold(text.replace("*", "")).split())


def unchanged(reply: str) -> bool:
    m = _JSON.search(reply or "")
    try:
        return (json.loads(m.group(0)) if m else {}).get("changed") is False
    except (ValueError, AttributeError):
        return False


def rule_gate(rulebook: str, cache_path: Path, generate: Callable[[str, list[str]], str]) -> frozenset[str]:
    """Names of the definitions that no longer hold under this rulebook. The model's verdicts are saved in cache_path."""
    clauses = parse_clauses(rulebook)
    path = Path(cache_path)
    verdicts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    broken = set()
    for name, tested in TESTED.items():
        text = clauses.get(fold(name))
        if text is None:
            broken.add(name)
        elif _plain(text) not in {_plain(t) for t in tested}:
            if text not in verdicts:
                parts = [f"Rule: {name}", f"Old text: {tested[-1]}", f"New text: {text}"]
                verdicts[text] = unchanged(generate(SYSTEM, parts))
            if not verdicts[text]:
                broken.add(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1), encoding="utf-8")
    return frozenset(c for c, needs in DEPENDS.items() if broken & set(needs))


def rule_note(off: frozenset[str]) -> str:
    """Prompt text for the definitions that are off."""
    if not off:
        return ""
    return ("\n\n# Changed rules\nThis rulebook changed the rules behind: " + ", ".join(sorted(off)) + ". The helpers of those names and "
            "the `counted` column still follow the old rule. For these quantities compute from the raw columns as the rulebook above says.")
