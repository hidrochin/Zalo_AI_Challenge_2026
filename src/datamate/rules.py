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
    # macros of the SQL store (sql/store.py): each encodes the train rulebook clauses it is named with
    'debt_by_order': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ'),
    'debt_total': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ'),
    'debt_age_sum': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ'),
    'debt_by_customer': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ'),
    'debt_customers': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ'),
    'debt_new': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ', 'Nợ thêm, trả nợ, thu nợ trong kỳ'),
    'debt_new_of': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ', 'Nợ thêm, trả nợ, thu nợ trong kỳ'),
    'debt_repaid': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ', 'Nợ thêm, trả nợ, thu nợ trong kỳ'),
    'debt_repaid_of': ('Đơn được tính vào nợ', 'Nợ một đơn tại ngày X', 'Nợ theo khách', 'Đơn COD, đơn bán qua sàn TMĐT', 'Tuổi nợ', 'Nợ thêm, trả nợ, thu nợ trong kỳ'),
    'delivery_trips': ('Giao nhiều đợt',),
    'import_total': ('Tiền nhập hàng trong kỳ',),
    'supplier_stats': ('Tiền nhập hàng trong kỳ',),
    'avg_import_price': ('Giá nhập bình quân một mặt hàng trong tháng',),
    'import_minus_sales': ('Nhập trừ bán, chênh nhập và bán', 'Đơn tính vào doanh thu'),
    'stock_split': ('Tồn theo đơn vị lớn',),
    'stock_value': ('Giá trị hàng tồn kho',),
    'out_of_stock': ('Mặt hàng hết hàng cuối ngày',),
    'slow_products': ('Mặt hàng không bán được trong kỳ (ế)',),
    'price_on': ('Giá bán X ngày D',),
    'price_effect': ('Hiệu ứng tăng giá',),
    'price_effect_at': ('Hiệu ứng tăng giá',),
    'price_increase_day': ('Hiệu ứng tăng giá',),
    'gross_profit': ('Lãi gộp', 'Đơn tính vào doanh thu'),
    'net_profit': ('Lãi gộp', 'Lãi ròng một tháng', 'Đơn tính vào doanh thu'),
    'size_qty': ('Size M, size L',),
    'expiring_lots': ('Hàng hết hạn trong N ngày tới',),
    'qty_unit': ('Bán được bao nhiêu <đơn vị>', 'Đơn tính vào doanh thu'),
    'share_of': ('Doanh thu theo nhóm khách', 'Đơn tính vào doanh thu', 'Doanh thu một đơn'),
    'customer_stats': ('Xác định khách', 'Số đơn', 'Đơn tính vào doanh thu'),
    'item_stats': ('Bán chạy', 'Số đơn', 'Đơn tính vào doanh thu'),
}
# Clause texts the definitions were checked against: the train rulebook only (contracts on gold). No other split's
# wording may be stored here: a rulebook of another split is not a development resource.
TESTED: dict[str, tuple[str, ...]] = {
    'Đơn tính vào doanh thu': (
        'đơn **không huỷ** và **không trả hàng**. Đơn có trả hàng, **kể cả chỉ trả một phần**, là đơn trả hàng: bỏ **cả đơn**. Đơn đang giao vẫn tính',
    ),
    'Doanh thu một đơn': (
        'tổng **thành tiền các dòng hàng** (đã trừ giảm giá trên từng dòng). **Không** trừ giảm giá trên cả đơn (khuyến mãi theo tổng tiền đơn, voucher shop), **không** cộng thuế VAT, **không** gồm phí giao hàng. **Tiền hàng**, **tiền bán** của đơn bán cùng nghĩa với doanh thu',
    ),
    'Đơn thuộc kỳ nào': (
        'theo **ngày đặt**, không theo ngày giao hay ngày thu tiền',
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
    'Bán chạy': (
        'theo **số lượng quy về đơn vị cơ bản**, không theo số dòng hay số tiền; bỏ đơn huỷ, trả. Mặt hàng khác đơn vị cơ bản vẫn xếp chung theo số đó. **Bán nhiều nhất**, **bán được nhiều nhất**, **hot nhất** cùng nghĩa với **bán chạy nhất**',
    ),
    'Bán được bao nhiêu <đơn vị>': (
        'cộng số lượng quy về đơn vị cơ bản của các mặt hàng **có đơn vị cơ bản là <đơn vị>**, kể cả dòng bán theo đơn vị lớn; mặt hàng có đơn vị cơ bản khác không tính',
    ),
    'Doanh thu theo nhóm khách': (
        'nhóm theo cột nhóm khách. Đơn không ghi khách (khách vãng lai) **không thuộc nhóm nào**, kể cả nhóm `lẻ`. Tỉ trọng của nhóm chia cho doanh thu **cả shop** cùng kỳ, tính cả đơn vãng lai',
    ),
    'Giao nhiều đợt': (
        '**Số đợt giao trong tháng X** = số đợt có ngày giao trong tháng X; **mỗi đợt của mỗi đơn tính một đợt**, kể cả đợt của đơn sau đó trả hàng',
    ),
    'Giá bán X ngày D': (
        'đơn giá ghi trên dòng bán X của các đơn đặt ngày D, cho **một đơn vị cơ bản**, **trước** chiết khấu dòng; tính cả dòng của đơn huỷ, đơn trả',
    ),
    'Giá nhập bình quân một mặt hàng trong tháng': (
        'bình quân **theo số lượng nhập**, **không** phải trung bình cộng các mức giá nhập; theo đơn vị nhập, các phiếu có ngày phiếu trong tháng, bỏ phiếu huỷ',
    ),
    'Giá trị hàng tồn kho': (
        'tồn **hết hôm nay** của từng mặt hàng nhân **giá vốn một đơn vị cơ bản ghi trong danh mục hàng**; chỉ tính hàng còn tồn',
    ),
    'Hiệu ứng tăng giá': (
        'lần tăng giá = ngày D giá mới cao hơn giá liền trước (theo bảng giá); tăng lên cùng một giá nhiều lần thì lấy lần đầu. So số lượng (đơn vị cơ bản) **N ngày kể từ D** (tính cả D) với **N ngày ngay trước D**; cả hai khoảng bỏ đơn huỷ và đơn trả',
    ),
    'Hàng hết hạn trong N ngày tới': (
        'tính tại hôm nay D: lô còn tồn có hạn dùng từ D đến hết ngày D + N. **Tiền vốn** là giá trị phần **còn tồn** của lô, theo giá nhập của chính lô đó quy về đơn vị cơ bản, không theo giá vốn bình quân',
    ),
    'Lãi gộp': (
        'doanh thu (§1) trừ giá vốn của hàng đã bán. Giá vốn của mỗi dòng bán bằng số lượng nhân với **giá vốn ghi trên dòng bán** (cho **một đơn vị bán**, cùng đơn vị với đơn giá). Không tự tính lại giá vốn từ phiếu nhập hay từ lô',
    ),
    'Lãi ròng một tháng': (
        'lãi gộp của tháng trừ **mọi khoản** ghi trong sổ chi phí của tháng đó, kể cả sửa chữa, thay thiết bị',
    ),
    'Mặt hàng hết hàng cuối ngày': (
        'mặt hàng **mua về để bán** (không tính món pha chế, chế biến tại chỗ) mà lúc **hết ngày** đó tồn kho bằng 0; hết giữa ngày nhưng nhập thêm trước cuối ngày thì không tính',
    ),
    'Mặt hàng không bán được trong kỳ (ế)': (
        'mặt hàng cửa hàng **đã từng bán trước kỳ** (có trong đơn §1, không tính đơn huỷ, đơn trả) nhưng trong kỳ không có đơn nào (§1) mua mặt hàng đó',
    ),
    'Nhập trừ bán, chênh nhập và bán': (
        'phần **bán** là số lượng của đơn tính vào doanh thu (§1), quy về cùng đơn vị với phần nhập',
    ),
    'Nợ một đơn tại ngày X': (
        'phần chưa trả của **tổng đơn** (số khách phải trả cả đơn: đã trừ giảm giá trên cả đơn, **gồm phí giao**), tính các khoản trả đến hết ngày X. Còn nợ = cộng các đơn **còn nợ > 0**',
    ),
    'Nợ theo khách': (
        'xác định khách như §3. Đơn **khách lẻ** vẫn tính vào **tổng nợ**, nhưng không có dòng trong bảng nợ theo khách',
    ),
    'Nợ thêm, trả nợ, thu nợ trong kỳ': (
        'chỉ đơn **ghi nợ**, không huỷ. **Nợ thêm**: phần tổng đơn chưa trả ngay trong ngày mua, của đơn đặt trong kỳ. **Trả nợ / thu nợ**: các khoản trả **sau ngày mua** có ngày trả trong kỳ, của mọi đơn ghi nợ (kể cả đơn kỳ trước, đơn trả hàng). Tiền trả cùng ngày mua là trả lúc mua. Hoàn tiền trả hàng không trừ',
    ),
    'Size M, size L': (
        'món có tên kết thúc bằng `(M)` hoặc `(L)`, **không phân biệt hoa thường**',
    ),
    'Tiền nhập hàng trong kỳ': (
        'tổng tiền các phiếu nhập có **ngày phiếu** trong kỳ, bỏ phiếu huỷ. Phiếu, dòng ghi **nhập đầu kỳ** là phiếu nhập như mọi phiếu khác: có ngày phiếu trong kỳ thì tính. Nhà cung cấp nhập nhiều nhất = nhà cung cấp có tiền nhập cao nhất',
    ),
    'Tuổi nợ': (
        'số ngày tính từ **ngày đặt**, không từ hạn thanh toán. Đơn đặt hôm nay có tuổi nợ **0** ngày, đơn đặt hôm qua **1** ngày',
    ),
    'Tồn theo đơn vị lớn': (
        'list 2 số `[số đơn vị lớn nguyên, số đơn vị cơ bản lẻ]`; không có phần lẻ thì số thứ hai là 0. Hàng nhiều cấp: phần lẻ vẫn tính bằng đơn vị cơ bản, không đổi sang đơn vị ở giữa',
    ),
    'Xác định khách': (
        'Shop có mã khách: mỗi **mã khách** là một khách, hai mã trùng tên là hai khách; đơn không ghi mã (**khách lẻ**) không phải khách, không đếm và không xếp hạng. Shop không có mã khách: xác định khách theo **tên ghi trên đơn**: hai tên **chỉ khác cách viết** là cùng một khách, hai tên **khác chữ hoặc khác số** là hai khách; đơn **khách lẻ** không tính. Khách mua **nhiều tiền** nhất xếp theo doanh thu (§1).',
    ),
    'Đơn COD, đơn bán qua sàn TMĐT': (
        '**không phải khách nợ**, kể cả khi chưa thu được tiền; bỏ khỏi công nợ và tuổi nợ',
    ),
    'Đơn được tính vào nợ': (
        'đơn không huỷ; **đơn trả hàng vẫn tính** nếu còn nợ',
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


_SHIFT = re.compile(r"ca (sang|chieu)\D{0,12}?(\d{1,2})\s*[:h]?\s*(\d{2})?\s*(?:-|–|den|toi)\s*(\d{1,2})\s*[:h]?\s*(\d{2})?")
SHIFT_MACROS = ("shift_revenue", "shift_orders")


def rule_params(rulebook: str) -> dict[str, int | None]:
    """Numbers the macros take from the rulebook, read from its text: the shift hours of 'Ca sáng, ca chiều'.
    A shift whose hours cannot be read gets None, so its macros are off and the model reads the rule itself."""
    text = parse_clauses(rulebook).get(fold("Ca sáng, ca chiều"), "")
    out: dict[str, int | None] = {}
    for name, key in (("sang", "morning"), ("chieu", "afternoon")):
        m = next((m for m in _SHIFT.finditer(fold(text)) if m.group(1) == name), None)
        lo = hi = None
        if m:
            lo, hi = int(m.group(2)), int(m.group(4))
            hi = hi - 1 if (m.group(5) or "00") == "00" and hi > lo else hi  # 06:00–14:00 ends before 14:00; 13:59 is in hour 13
        out[f"shift_{key}_from"], out[f"shift_{key}_to"] = lo, hi
    return out


def params_off(params: dict[str, int | None]) -> frozenset[str]:
    """The macros whose rulebook numbers could not be read."""
    return frozenset(SHIFT_MACROS) if any(v is None for v in params.values()) else frozenset()


def rule_note(off: frozenset[str]) -> str:
    """Prompt text for the definitions that are off."""
    if not off:
        return ""
    return ("\n\n# Changed rules\nThis rulebook changed the rules behind: " + ", ".join(sorted(off)) + ". The helpers of those names and "
            "the `counted` column still follow the old rule. For these quantities compute from the raw columns as the rulebook above says.")
