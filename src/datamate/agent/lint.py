"""A look at a program before it runs: the question is a term with a tested helper, or the program counts at the wrong grain.

The model tends to rebuild a rulebook term from raw tables, where it fails most. This asks for one rewrite and never
blocks: a second attempt runs as written, so a question that only looks like a term is not lost.
"""

import re

from datamate.eval.factcheck import group_of
from datamate.facts import Facts
from datamate.text import fold

# question group -> (pandas helpers, SQL macros, facts they need)
TERMS = {
    "debt_aged": (("debt_orders",), ("debt_by_order",), ("orders", "payments")),
    "debt_flow": (("debt_new", "debt_repaid"), ("debt_new", "debt_new_of", "debt_repaid", "debt_repaid_of"), ("orders", "payments")),
    "debt_repaid": (("debt_repaid",), ("debt_repaid",), ("orders", "payments")),
    "debt_count": (("debt_customers",), ("debt_by_customer",), ("orders", "payments")),
    "debt_top": (("debt_customers", "debt_total", "debt_orders"), ("debt_by_customer", "debt_total", "debt_by_order"), ("orders", "payments")),
    "debt_total": (("debt_total", "debt_orders"), ("debt_total", "debt_by_order"), ("orders", "payments")),
    "delivery_trips": (("delivery_trips",), ("delivery_trips",), ("deliveries",)),
    "avg_import_price": (("avg_import_price",), ("avg_import_price",), ("imports",)),
    "gross_profit": (("gross_profit",), ("gross_profit",), ("cost_col",)),
    "best_margin": (("gross_profit",), (), ("cost_col",)),  # SQL gross_profit() is one shop-wide number: it cannot rank items
    "net_profit": (("net_profit",), ("net_profit",), ("cost_col", "expenses")),
    "slow_products": (("slow_products",), ("slow_products",), ()),
    "price_effect": (("price_effect",), ("price_effect",), ()),
    "nearest_lot": (("lots_left",), ("lots_left",), ("lots", "lot_sales")),
}
# SQL formula catalog: question shape -> the store macros that compute it (sql/store.py), tested on train and public labels.
# Checked before the fact groups; a group is offered only where the shop's store has all of its macros.
CATALOG = [
    ("items_min_orders", r"(mat hang|mon|san pham|\bsp\b|hang)\b.*\b(co mat|gop mat|xuat hien|nam trong|co trong|duoc (goi|mua|dat)|goi)\b.*\d+ (don|hoa don)", ("items_in_min_orders",)),
    ("customers_min_orders", r"\b(tu|it nhat|toi thieu|>=?|≥) ?\d+ (don|hoa don|lan)\b|\b\d+ (don|hoa don|lan) tro len\b", ("share_customers_min_orders", "customers_min_orders")),
    ("debt_aging", r"\btuoi no\b", ("debt_aging",)),
    ("debt_count", r"(bao nhieu|may|bn|so|dem( dup| giup)?( cac)?) (khach|nguoi)\b.*\b(no|cong no|thieu)\b", ("debt_customers",)),
    ("price_effect", r"doi gia|tang gia|len gia", ("price_effect_at", "price_effect")),
    ("shift", r"\bca (sang|chieu)\b", ("shift_revenue", "shift_orders")),
    ("customer_rank", r"\bkhach\b.*\b(trung thanh|sop|hay doi y|mua nhieu (tien|lan|don)|chi nhieu)", ("customer_stats",)),
    ("group_aov", r"\bnhom\b.*\b(trung binh|binh quan)\b|\b(trung binh|binh quan)\b.*\bnhom\b", ("avg_order_value_group",)),
    ("item_rank", r"\bua chuong\b|(mat hang|mon|san pham|hang)\b.*\b(loi|lai)( gop)? (nhat|nhieu nhat|cao nhat)", ("item_stats",)),
    ("day_count", r"\b(may|bao nhieu|bn) (ngay|bua)\b.*\b(tu|tren|tro len|hon)\b|\bngay (dat khach|dong khach)\b", ("day_stats",)),
    ("month_rank", r"\bthang nao\b|thang sau.*(cao|hon).*thang truoc|tang (deu|lien tuc)", ("month_stats",)),
    ("peak_hour", r"gio cao diem|khung gio", ("hour_stats",)),
    ("import_minus", r"nhap.*\btru\b.*ban|chenh.*nhap.*ban|nhap vao tru", ("import_minus_sales",)),
    ("stock_split", r"\b(chan|nguyen)\b.*\ble\b|\ble\b.*\b(chan|nguyen)\b", ("stock_split",)),
    ("stock_value", r"\bgia von\b.*\bkho\b|\bkho\b.*\bgia von\b|gia tri (hang )?ton", ("stock_value",)),
    ("expiring", r"het han trong \d+ ngay|han (dung|su dung).*\btu\b.*\b(den|toi)\b|sap het han", ("expiring_lots",)),
    ("size", r"\bsize (m|l|s)\b", ("size_qty",)),
    ("model_line", r"\bdong may\b|\bmay (iphone|samsung|xiaomi|oppo|vivo|realme|nokia)\b|\biphone \d+", ("model_line", "brand_of")),
    ("extra_lines", r"\bvoucher\b|phi ship|\b(dung|tru) diem\b|\bdiem\b.*\b(tru|giam)\b", ("extra_lines",)),
]
CATALOG = [(n, re.compile(p), m) for n, p, m in CATALOG]


def catalog_group(text: str, macros) -> tuple[str, tuple[str, ...]] | None:
    """The first catalog group the question matches whose macros this store has."""
    f = fold(text)
    return next(((n, m) for n, rx, m in CATALOG if rx.search(f) and all(x in macros for x in m[:1])), None)


_ASKS_ORDERS = re.compile(r"\b(bao nhieu|may|so|bn|nhieu) (don|hoa don)\b")
_ABOUT_SALES = re.compile(r"doanh thu|ban (duoc|ra)|\bdon\b")
_ANY_STATUS = re.compile(r"\bhuy\b|tra hang|gia ban|don gia")
_COUNTS_ORDERS = re.compile(r"\borders\(\)|\border_id\b")
_HAS_RULE = re.compile(r"\bcounted\b|\b(revenue|avg_order_value|price_on|gross_profit|price_effect|slow_products)\(")


def ready_terms(facts: Facts | None) -> frozenset[str]:
    """Question groups this shop has a working helper for."""
    if facts is None:
        return frozenset()
    return frozenset(g for g, (_, _, needs) in TERMS.items() if all(getattr(facts, n) is not None for n in needs))


def term_advice(text: str, code: str, terms: frozenset[str], engine: str, macros=()) -> str | None:
    """Which helper the question calls for, when the program uses none of them."""
    hit = catalog_group(text, macros) if engine == "sql" and macros else None
    if hit:
        names = [m for m in hit[1] if m in macros]
        if any(re.search(rf"\b{n}\s*\(", code) for n in names):
            return None
        return (f"This question matches the rulebook formula {' / '.join(names)} (see Macros), tested on the data. Call it "
                "instead of rebuilding the logic, unless the question really asks for something else.")
    group = group_of(text)
    if group not in terms:
        return None
    names = TERMS[group][0 if engine == "pandas" else 1]
    if not names or any(re.search(rf"\b{n}\s*\(", code) for n in names):
        return None
    return (f"This question looks like the rulebook term behind {' / '.join(names)}, a tested definition. Call it instead of rebuilding the "
            "logic from raw tables, unless the question really asks for something else.")


def grain_advice(text: str, code: str) -> str | None:
    """A pandas program that counts lines as orders or drops the default sales rule."""
    f = fold(text)
    if "sales_lines" not in code:
        return None
    if _ASKS_ORDERS.search(f) and not _COUNTS_ORDERS.search(code):
        return ("sales_lines() has one row per product line, so summing a mask counts lines, not orders. "
                "Use orders() or count distinct order_id with nunique().")
    if _ABOUT_SALES.search(f) and not _ANY_STATUS.search(f) and not _HAS_RULE.search(code):
        return "Filter on s['counted']: it drops cancelled and returned orders, as the rulebook's default rule says."
    return None
