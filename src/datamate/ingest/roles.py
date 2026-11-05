"""Map columns to a few canonical roles from README descriptions (fallback: header names).

Roles drive deterministic cleaning (forward-fill by order id, dedupe, status normalization)
and are shown to the LLM in the schema card.
"""

import re

from datamate.text import fold

# (role, regex on the folded README description). First match wins.
DESC_RULES: list[tuple[str, str]] = [
    ("qty_base", r"^so luong (da )?(doi|quy) ra don vi co ban|^so luong .*\(dvt co ban\)|^so luong .*\(don vi co ban\)"),
    ("order_id", r"^ma (don|hoa don|bill|chung tu)\b(?!.*nhap)|^so (hoa don|hd)\b|^so thu tu (don|hoa don)\b"),
    ("order_date", r"^ngay (ban|dat|giao dich|mua)"),
    ("order_ts", r"^(thoi diem|thoi gian|gio) (dat|tao) don|^gio dat don"),
    ("status", r"^trang thai (don|xu ly)"),
    ("product_id", r"^ma (hang|san pham|sku|mat hang)"),
    ("product_name", r"^ten (hang|san pham|mon|mat hang)"),
    ("qty", r"^so luong( ban)?$|^so luong \(|^so luong ghi"),
    ("unit", r"^don vi tinh|^dvt\b"),
    ("base_unit", r"^don vi co ban"),
    ("unit_price", r"^don gia"),
    ("line_amount", r"^thanh tien"),
    ("customer_name", r"^ten khach"),
    ("customer_id", r"^ma khach"),
]

HEADER_RULES: list[tuple[str, str]] = [
    ("order_id", r"^(ma (don hang|hoa don|don|bill|hd|giao dich)|so (hd|hoa don|phieu ban|ct|chung tu)|don so|order id)$"),
    ("status", r"^(trang thai( don hang| don| giao)?|tinh trang( don)?|status)$"),
    ("order_ts", r"^(thoi gian (lap|tao)( don| hoa don)?|gio (tao|dat|lap) (don|hoa don))$"),
    ("order_date", r"^(ngay (ban|dat hang|giao dich|mua|lap|hd|hoa don)|ngayban|ngay|thoi gian|ngay gio)$"),
    ("product_id", r"^ma (hang|sp|san pham|hh|hang hoa|mat hang|sku)$"),
    ("product_name", r"^(ten (hang|san pham|hang hoa|mat hang|sp)|mat hang|san pham|hang hoa|hang ban|hang)$"),
    ("qty_base", r"^(so luong|sl|s\.l) (le|quy doi|co ban)$|^(so luong|sl|s\.l) \((dv|don vi|dvt) co ban\)$"),
    ("qty", r"^(so luong|sl|s\.l|slg)$"),
    ("unit", r"^(dvt|dv|don vi( tinh)?)$"),
    ("unit_price", r"^(don gia|gia ban|gia don vi|gia 1 dv)$"),
    ("line_amount", r"^(thanh tien|t\.? ?tien|tri gia|amount)( \((d|vnd)\))?$"),
    ("customer_id", r"^(ma (kh|khach( hang)?)( mua)?|customer id)$"),
    ("customer_name", r"^((ten )?khach hang( \(ten\))?|ten (kh|khach)|khach)$"),
]
# A README description that matches no DESC rule falls back to the header, except for money roles:
# "Thành tiền" described as "tổng tiền đơn" is an order total, not a line amount.
_NO_HEADER_FALLBACK = {"line_amount", "unit_price"}
# Generic money headers mean the line amount only in a table that also has product lines.
_LINE_AMOUNT_IF_PRODUCT = re.compile(r"^(tong tien|tien|tien ban|tien hang)$")

_DESC = [(role, re.compile(p)) for role, p in DESC_RULES]
_HEAD = [(role, re.compile(p)) for role, p in HEADER_RULES]


def header_key(column: str) -> str:
    """Folded header with CamelCase and snake_case split into words: 'SLQuyDoi' -> 'sl quy doi'."""
    s = str(column)
    s = "".join(" " + c if i and c.isupper() and (s[i - 1].islower() or s[i - 1].isupper() and s[i + 1:i + 2].islower())
                else c for i, c in enumerate(s))
    return fold(s.replace("_", " "))


def role_for(column: str, desc: str | None) -> str | None:
    if desc:
        d = fold(desc)
        for role, rx in _DESC:
            if rx.search(d):
                return role
    h = header_key(column)
    for role, rx in _HEAD:
        if rx.search(h):
            return None if desc and role in _NO_HEADER_FALLBACK else role
    return None


def assign_roles(columns: list[str], descs: dict[str, str], order_keys: set[str] = frozenset()) -> dict[str, str]:
    """Return {column: role}; each role is given to at most one column (first wins).

    order_keys: columns the README names as the order key ("các dòng cùng **STT** là một đơn")."""
    folded = {fold(k): v for k, v in descs.items()}
    out: dict[str, str] = {}
    used: set[str] = set()
    for c in columns:
        if fold(c) in order_keys:
            out[c] = "order_id"
            used.add("order_id")
            break
    for c in columns:
        if c in out:
            continue
        desc = descs.get(c) or folded.get(fold(c))
        role = role_for(c, desc)
        if role and role not in used:
            out[c] = role
            used.add(role)
    for c in columns:  # a bare "tên" (description or header) names the table's entity: product or customer
        desc = descs.get(c) or folded.get(fold(c))
        is_name = fold(desc).strip(" .") == "ten" if desc else header_key(c) in ("ten", "ten goi")
        if c in out or not is_name:
            continue
        role = "product_name" if "product_id" in used else ("customer_name" if "customer_id" in used else None)
        if role and role not in used:
            out[c] = role
            used.add(role)
    if "line_amount" not in used and used & {"product_name", "product_id"}:
        for c in columns:
            desc = descs.get(c) or folded.get(fold(c))
            if c not in out and not desc and _LINE_AMOUNT_IF_PRODUCT.match(fold(c)):
                out[c] = "line_amount"
                break
    return out


STATUS_LABELS = ("hoàn thành", "huỷ", "trả hàng", "đang giao")
# Folded values of a finished order. normalize_status also lands on 'hoàn thành' for anything it cannot place,
# so a value outside this list is unknown, not finished: agent/statuses.py asks the model about it.
_DONE = frozenset({"hoan thanh", "da hoan thanh", "hoan tat", "da hoan tat", "xong", "da xong", "completed", "done", "ok",
                   "thanh cong", "success", "da lay", "da giao", "da giao hang", "delivered", "da nhan", "da nhan hang",
                   "received", "dang xu ly"})


def normalize_status(value) -> str | None:
    """Map a raw status string to one of the rulebook's four statuses."""
    if value is None:
        return None
    s = fold(value)
    if not s:
        return None
    # word boundaries: "chuyen" (Đang vận chuyển, chuyển khoản) and "Huỳnh" contain "huy"
    if re.search(r"\bhuy\b|cancel|khong giao duoc|giao (that bai|khong thanh cong)|failed", s):
        return "huỷ"
    if re.search(r"\btra (hang|lai|ve)\b|hoan (hang|tra)|return|doi tra|khach (mang )?tra|mang tra", s):
        return "trả hàng"
    if re.search(r"dang (giao|van chuyen|ship|gui)|chua giao|cho giao|shipping|delivering", s):
        return "đang giao"
    return "hoàn thành"


def is_known_status(value) -> bool:
    return fold(value) in _DONE or normalize_status(value) != "hoàn thành"
