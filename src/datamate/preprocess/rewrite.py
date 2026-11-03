"""One short LLM call that rewrites the rule-cleaned question into standard Vietnamese and reads off its
answer type, top-k and sub-questions.

The rewrite is checked before use: it must keep every number of the rule-cleaned text and every name that
matched the shop's data exactly. A rewrite that fails the check is dropped (the type guess is kept), so the
solver falls back to the rule-cleaned text. Few-shot examples are train questions or written by hand.
"""

import json
import re
from collections import Counter

from datamate.preprocess import RulesResult
from datamate.text import fold, norm_key

SYSTEM = """You normalize questions that small-shop owners ask about their own sales data. Rewrite the question into clear, standard Vietnamese with full diacritics. Do NOT answer it.

Rules:
- Keep every number, date, period, amount, unit and condition exactly. Do not add, drop or change anything the question asks.
- Expand abbreviations, teen-code, dialect and English words (dthu -> doanh thu, bn -> bao nhiêu, mô -> nào, ni -> này, rứa -> vậy, revenue -> doanh thu, bill -> đơn, hot -> bán chạy).
- Fix typos and missing diacritics in ordinary words. Drop greetings and filler ("ad ơi", "cho hỏi", "z", "nha", emoji).
- Names: when "names in the shop's data" lists an exact name for a phrase, write that exact name. Otherwise copy product, customer and staff names exactly as written in the question: keep every word, do not add or change diacritics.
- Never add a quantity, unit, condition or period that the question does not state.

Return ONLY one JSON object, no other text:
{"normalized": "...", "answer_type": "...", "top_k": null, "sub_questions": [], "entities": [{"text": "...", "kind": "..."}]}
- answer_type: "percent" (%, tỉ lệ, tỉ trọng, tăng/giảm bao nhiêu %), "date" (which day: ngày nào), "month" (which month: tháng nào), "boolean" (yes/no: có ... không, phải không, tăng liên tục không), "list" (several names or numbers in a given order: top-k, "lần lượt", several periods or quantities), "table" (rows per object with amounts: "kèm số tiền", "mỗi người còn nợ bao nhiêu"), "string" (one name: mặt hàng nào / khách nào ... nhất), else "number" (money, counts, quantities).
- top_k: the k of "top k" / "k mặt hàng ... nhất", else null.
- sub_questions: only when several things are asked, in the order asked; else [].
- entities: product / customer / staff / supplier / group names the question mentions, as written in the normalized question; kind is one of product, customer, staff, supplier, group, other.

Examples:

question: thangs 8/2025 bán Yakult 65ml dduwowcj bao nhiêu tiền
pre-cleaned: Tháng 8/2025 bán Yakult 65ml được bao nhiêu tiền
{"normalized": "Tháng 8/2025 bán Yakult 65ml được bao nhiêu tiền?", "answer_type": "number", "top_k": null, "sub_questions": [], "entities": [{"text": "Yakult 65ml", "kind": "product"}]}

question: top 3 sp ban nhieu nhat thang 3 nam 2026 la nhung j z
pre-cleaned: Top 3 sản phẩm ban nhieu nhat thang 3 nam 2026 la nhung gì vậy
{"normalized": "Top 3 sản phẩm bán nhiều nhất tháng 3 năm 2026 là những sản phẩm nào?", "answer_type": "list", "top_k": 3, "sub_questions": [], "entities": []}

question: so vs th truoc do, revenue thang 1/2026 tang hay giam may %
pre-cleaned: So với tháng truoc do, doanh thu tháng 1/2026 tang hay giam may %
{"normalized": "So với tháng trước đó, doanh thu tháng 1/2026 tăng hay giảm bao nhiêu %?", "answer_type": "percent", "top_k": null, "sub_questions": [], "entities": []}

question: thang nao dthu cao nhat trong ba thang 2/2025, 3/2025, 4/2025
pre-cleaned: Thang nao doanh thu cao nhat trong ba tháng 2/2025, tháng 3/2025, tháng 4/2025
{"normalized": "Trong ba tháng 2/2025, 3/2025 và 4/2025, tháng nào có doanh thu cao nhất?", "answer_type": "month", "top_k": null, "sub_questions": [], "entities": []}

question: Doanh thu 3 tháng 5/2025, 6/2025, 7/2025 có tăng liên tục ko?
pre-cleaned: Doanh thu 3 tháng 5/2025, tháng 6/2025, tháng 7/2025 có tăng liên tục không?
{"normalized": "Doanh thu 3 tháng 5/2025, 6/2025, 7/2025 có tăng liên tục không?", "answer_type": "boolean", "top_k": null, "sub_questions": [], "entities": []}

question: khach Phung Dat Trang thang 2/2025 no them bao nhieu, tra duoc bao nhieu
pre-cleaned: Khach Phùng Đạt Trang tháng 2/2025 no them bao nhieu, tra duoc bao nhieu
names in the shop's data:
- customer: "Phùng Đạt Trang" (exact, for "Phung Dat Trang")
{"normalized": "Tháng 2/2025 khách Phùng Đạt Trang nợ thêm bao nhiêu tiền và trả được bao nhiêu tiền?", "answer_type": "list", "top_k": null, "sub_questions": ["Tháng 2/2025 khách Phùng Đạt Trang nợ thêm bao nhiêu tiền?", "Tháng 2/2025 khách Phùng Đạt Trang trả nợ được bao nhiêu tiền?"], "entities": [{"text": "Phùng Đạt Trang", "kind": "customer"}]}

question: cho bảng 3 khách còn nowj nhiều nhất tính ddeens hết ngày 30/06/2026, kèm số tiền còn nợ
pre-cleaned: Cho bảng 3 khách còn nợ nhiều nhất tính đến hết ngày 30/06/2026, kèm số tiền còn nợ
{"normalized": "Liệt kê 3 khách hàng còn nợ nhiều nhất tính đến hết ngày 30/06/2026, kèm số tiền còn nợ của từng người.", "answer_type": "table", "top_k": 3, "sub_questions": [], "entities": []}

question: tuần ni quán mần được mấy bill rứa
pre-cleaned: Tuần này quán làm được mấy đơn vậy
{"normalized": "Tuần này quán bán được bao nhiêu đơn?", "answer_type": "number", "top_k": null, "sub_questions": [], "entities": []}

question: thang 3/2026 co may don dthu tu 2tr5 tro len
pre-cleaned: Tháng 3/2026 co may don doanh thu tu 2.500.000 đồng tro len
{"normalized": "Tháng 3/2026 có bao nhiêu đơn có doanh thu từ 2.500.000 đồng trở lên?", "answer_type": "number", "top_k": null, "sub_questions": [], "entities": []}
"""

_JSON = re.compile(r"\{.*\}", re.S)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
ANSWER_TYPES = {"number", "percent", "string", "date", "month", "boolean", "list", "table"}


def names_block(rules: RulesResult) -> str:
    by_span: dict[tuple[int, int], list] = {}
    for m in rules.mentions:
        by_span.setdefault((m.start, m.end), []).append(m)
    lines = []
    for ms in by_span.values():
        full = [m for m in ms if m.full]
        if full:
            lines.append(f'- {full[0].kind}: "{full[0].name}" (exact)')
        else:
            lines.append(f"- {ms[0].kind} (partial match, check): " + " | ".join(f'"{m.name}"' for m in ms))
    return "names in the shop's data:\n" + "\n".join(lines) if lines else ""


def user_message(question: str, rules: RulesResult) -> str:
    parts = [f"question: {question}", f"pre-cleaned: {rules.text}"]
    nb = names_block(rules)
    if nb:
        parts.append(nb)
    return "\n".join(parts)


# words that name data but also carry the question itself; a rewrite may rephrase them away
_COMMON = set("""cua hang ban mua khach don so la thang ngay nam tuan cho va co nhieu bao tien cai loai nhom ca gia tong
duoc trong mot cac nhung may ha nhi a the vay nhe nha oi ta e em anh chi xem gium giup hoi biet toi minh coi ra theo
tinh hom nay qua nao gi ai con het den tu khong bi o sao lam roi luon di voi tai ve mat san pham""".split())


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(s))


def _numbers(s: str) -> Counter:
    return Counter(re.sub(r"[.,]", "", n) for n in _NUMBER.findall(s))


def parse(reply: str, rules: RulesResult, vocab: frozenset[str] = frozenset()) -> dict | None:
    """The validated rewrite; 'normalized' is None when the rewrite changed a number, lost an exact name or
    dropped a word of the shop's names (`vocab`: folded words of every name in the shop's data)."""
    m = _JSON.search(reply or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    norm = d.get("normalized")
    out = {
        "normalized": norm if isinstance(norm, str) and norm.strip() else None,
        "answer_type": d.get("answer_type") if d.get("answer_type") in ANSWER_TYPES else None,
        "top_k": d.get("top_k") if isinstance(d.get("top_k"), int) and not isinstance(d.get("top_k"), bool) else None,
        "sub_questions": [s for s in d.get("sub_questions") or [] if isinstance(s, str)][:6],
        "entities": [e for e in d.get("entities") or [] if isinstance(e, dict) and isinstance(e.get("text"), str)][:8],
    }
    if out["normalized"]:
        problems = []
        if _numbers(out["normalized"]) != _numbers(rules.text):
            problems.append("numbers changed")
        lost = [m.name for m in rules.mentions if m.full and norm_key(m.name) not in norm_key(out["normalized"])]
        if lost:
            problems.append(f"lost names {lost}")
        kept = set(_words(out["normalized"]))
        dropped = sorted({w for w in _words(rules.text) if w in vocab and len(w) > 1 and w not in kept} - _COMMON)
        if dropped:
            problems.append(f"dropped name words {dropped}")
        if len(out["normalized"]) > 3 * len(rules.text) + 40:
            problems.append("too long")
        if problems:
            out["rejected"] = {"normalized": out["normalized"], "why": problems}
            out["normalized"] = None
    return out


def rewrite(generate, question: str, rules: RulesResult, vocab: frozenset[str] = frozenset()) -> tuple[dict | None, str]:
    """generate(system, user_parts) -> reply text. Returns (validated rewrite or None, raw reply)."""
    reply = generate(SYSTEM, [user_message(question, rules)])
    return parse(reply, rules, vocab), reply
