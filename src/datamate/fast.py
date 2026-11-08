"""Fast branch: answer a question without the model when a closed frame explains every word of it.

Closed world: metric words, the one resolved period and FILLER must cover the whole question, so a word the parser does
not know sends it to the model. Words that can change the meaning (riêng, hết, đến, từ, ở, trạng thái) are kept out of
FILLER on purpose. The vocabulary comes from train questions only.
"""

import re
from dataclasses import dataclass

import pandas as pd

from datamate import dm
from datamate.agent.hints import time_hints
from datamate.eval.semantic import Check, find_check
from datamate.ingest.trust import Trust
from datamate.text import fold

METRIC_WORDS = {
    "revenue": "doanh thu tong",
    "order_count": "so don hang hoa",
    "cancel_count": "so don hang bi huy",
    "cancel_rate": "ti ty le don hang bi huy phan tram %",
    "avg_order_value": "trung binh binh quan moi mot tren don hang hoa doanh thu gia tri",
}
FILLER = frozenset("bao nhieu la cho trong toi biet co cua duoc may mang ve xem hoi ban vay giup minh gium nhi da cong tinh dem coi ha a ta e".split())
CAPABILITY = {"revenue": "revenue", "order_count": "orders", "cancel_count": "orders", "cancel_rate": "orders", "avg_order_value": "orders"}
_TOKEN = re.compile(r"[a-z0-9%]+")
_HINT = re.compile(r'- "(.*?)" -> (.*?) = ')


@dataclass
class Frame:
    check: Check
    call: str


def parse_frame(text: str, as_of: str, cal: pd.DataFrame | None) -> Frame | None:
    """The frame of a rule-normalized question, or None when any word is left unexplained."""
    check = find_check(text, "percent") or find_check(text, "number")
    hints = time_hints(text, as_of, cal)
    m = _HINT.match(hints[0]) if check and len(hints) == 1 else None
    if m is None:
        return None
    known = FILLER | set(METRIC_WORDS[check.name].split())
    words = _TOKEN.findall(fold(text).replace(m.group(1), " ", 1))
    return Frame(check, m.group(2).split("  #")[0]) if all(w in known for w in words) else None


def fast_answer(text: str, as_of: str, cal: pd.DataFrame | None, sales: pd.DataFrame | None, trust: Trust,
                off: frozenset[str] = frozenset()) -> tuple[dict, Frame] | None:
    """(answer, frame) when every gate passes; call it outside worker threads, because dm keeps one context."""
    frame = parse_frame(text, as_of, cal)
    if frame is None or sales is None or frame.check.name in off or trust.states.get(CAPABILITY[frame.check.name]) != "trusted":
        return None
    dm.set_context(as_of, cal, sales)
    try:
        period = eval(frame.call, {n: getattr(dm, n) for n in dm.__all__})
        days = sales.loc[sales["date"] <= dm.AS_OF, "date"]
        if days.empty or period.end < days.min() or period.start > days.max():  # outside the data: the model decides
            return None
        value = frame.check.compute(period)
    except Exception:  # noqa: BLE001 - the primitive cannot compute it here: the model decides
        return None
    if pd.isna(value):
        return None
    return (dm.answer_percent(value) if frame.check.gold_type == "percent" else dm.answer_number(value)), frame
