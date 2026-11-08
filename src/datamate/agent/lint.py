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


def term_advice(text: str, code: str, terms: frozenset[str], engine: str) -> str | None:
    """Which helper the question calls for, when the program uses none of them."""
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
