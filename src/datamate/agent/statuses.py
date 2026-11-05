"""Order status values the rules do not know: one model call per shop, saved next to the ingest cache.

Every later load of the shop applies the saved labels, so pandas workers and the DuckDB store see the same statuses.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

from datamate.ingest.business import Business, status_path
from datamate.ingest.roles import STATUS_LABELS
from datamate.text import fold

SYSTEM = """You read order status values from a Vietnamese shop's records. Each order has exactly one of four statuses:
- hoàn thành: the customer got the goods.
- huỷ: the order did not go through (cancelled, delivery failed, customer refused the goods).
- trả hàng: goods were delivered, then returned, fully or in part.
- đang giao: the goods are on the way, not delivered yet.
Reply with one JSON object mapping each value exactly as written to one of these four statuses, or to null when the value does not clearly say which. Nothing else."""

_JSON = re.compile(r"\{.*\}", re.S)


def parse_labels(reply: str, values: list[str]) -> dict[str, str | None]:
    """Folded value -> label for the values asked about; anything else in the reply is dropped."""
    m = _JSON.search(reply or "")
    try:
        got = json.loads(m.group(0)) if m else {}
    except ValueError:
        got = {}
    got = {fold(k): v for k, v in got.items()} if isinstance(got, dict) else {}
    return {fold(v): got[fold(v)] if got.get(fold(v)) in STATUS_LABELS else None for v in values}


def resolve_statuses(biz: Business, cache_dir: Path, generate: Callable[[str, list[str]], str]) -> bool:
    """Ask once about this shop's unknown statuses. True when new labels were saved; load the shop again to get them."""
    values = sorted({v for ti in biz.info.values() for v in ti.odd_status})
    path = status_path(cache_dir, biz.split, biz.id)
    if not values or path.exists():
        return False
    parts = [f"Shop README:\n{biz.readme.text.strip()}", "Status values:\n" + "\n".join(f"- {v}" for v in values)]
    path.write_text(json.dumps(parse_labels(generate(SYSTEM, parts), values), ensure_ascii=False, indent=1), encoding="utf-8")
    return True
