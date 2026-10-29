"""Map columns to a few canonical roles from README descriptions (fallback: header names).

Roles drive deterministic cleaning (forward-fill by order id, dedupe, status normalization)
and are shown to the LLM in the schema card.
"""

import re

from datamate.text import fold

# (role, regex on the folded README description). First match wins.
DESC_RULES: list[tuple[str, str]] = [
    ("qty_base", r"^so luong (da )?(doi|quy) ra don vi co ban|^so luong .*\(dvt co ban\)|^so luong .*\(don vi co ban\)"),
    ("order_id", r"^ma (don|hoa don|bill|chung tu)\b(?!.*nhap)|^so (hoa don|hd)\b"),
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
    ("order_id", r"^(ma (don hang|hoa don|don|bill|hd)|so (hd|hoa don|phieu ban))$"),
    ("status", r"^(trang thai( don hang| don)?|tinh trang( don)?|status)$"),
    ("order_date", r"^(ngay (ban|dat hang|giao dich|mua)|ngayban)$"),
    ("product_name", r"^(ten (hang|san pham|hang hoa)|mat hang|san pham)$"),
    ("qty", r"^(so luong|sl)$"),
    ("customer_name", r"^(ten )?khach hang$"),
]

_DESC = [(role, re.compile(p)) for role, p in DESC_RULES]
_HEAD = [(role, re.compile(p)) for role, p in HEADER_RULES]


def role_for(column: str, desc: str | None) -> str | None:
    if desc:
        d = fold(desc)
        for role, rx in _DESC:
            if rx.search(d):
                return role
        return None
    h = fold(column)
    for role, rx in _HEAD:
        if rx.search(h):
            return role
    return None


def assign_roles(columns: list[str], descs: dict[str, str]) -> dict[str, str]:
    """Return {column: role}; each role is given to at most one column (first wins)."""
    folded = {fold(k): v for k, v in descs.items()}
    out: dict[str, str] = {}
    used: set[str] = set()
    for c in columns:
        desc = descs.get(c) or folded.get(fold(c))
        role = role_for(c, desc)
        if role and role not in used:
            out[c] = role
            used.add(role)
    return out


STATUS_LABELS = ("hoàn thành", "huỷ", "trả hàng", "đang giao")


def normalize_status(value) -> str | None:
    """Map a raw status string to one of the rulebook's four statuses."""
    if value is None:
        return None
    s = fold(value)
    if not s:
        return None
    if re.search(r"huy|cancel", s):
        return "huỷ"
    if re.search(r"tra (hang|lai)|hoan (hang|tra)|return|doi tra|khach tra", s):
        return "trả hàng"
    if re.search(r"dang (giao|van chuyen|ship)|chua giao|cho giao|shipping|delivering", s):
        return "đang giao"
    return "hoàn thành"
