"""Combine several executed samples into one decision: answer, unanswerable or abstain."""

import json
from collections import Counter

from datamate.text import norm_key


def _canon(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    if isinstance(v, str):
        return norm_key(v)
    if isinstance(v, list):
        return [_canon(x) for x in v]
    if isinstance(v, dict):  # table: rows are a set
        rows = sorted(json.dumps(_canon(r), ensure_ascii=False) for r in v.get("rows", []))
        return {"columns": [norm_key(c) for c in v.get("columns", [])], "rows": rows}
    return str(v)


def answer_key(result: dict) -> str:
    if result["type"] == "unanswerable":
        return "unanswerable"
    return json.dumps([result["type"], _canon(result.get("answer"))], ensure_ascii=False, sort_keys=True)


def decide(results: list[dict | None], min_share: float = 0.5, unans_share: float = 0.6) -> tuple[dict, dict]:
    """results: one entry per sample (None = failed). Share is computed over ALL samples."""
    n = len(results)
    ok = [r for r in results if r is not None]
    if not ok:
        return {"type": "abstain"}, {"n": n, "valid": 0, "share": 0.0, "reason": "no valid sample"}
    counts = Counter(answer_key(r) for r in ok)
    key, c = counts.most_common(1)[0]
    share = c / n
    first = next(r for r in ok if answer_key(r) == key)
    info = {"n": n, "valid": len(ok), "share": round(share, 3), "distinct": len(counts)}
    if key == "unanswerable":
        if share >= unans_share:
            return {"type": "unanswerable", "reason": first.get("reason", "")}, info
        return {"type": "abstain"}, {**info, "reason": "weak unanswerable vote"}
    if share >= min_share:
        return first, info
    return {"type": "abstain"}, {**info, "reason": "low agreement"}


def _agree_key(result: dict) -> str:
    """answer_key, but a table is its rows only: each engine names its columns its own way."""
    if result["type"] == "table":
        rows = sorted(json.dumps(_canon(r), ensure_ascii=False) for r in (result.get("answer") or {}).get("rows", []))
        return json.dumps(["table", rows], ensure_ascii=False)
    return answer_key(result)


def combine(finals: dict[str, dict]) -> tuple[dict, dict]:
    """One final answer from several engines' finals: the shared answer when they agree, the only answer when the
    others abstain, and abstain when two disagree. On train and public, agreeing answers were right 99% and 94% of
    the time, and each side of a disagreement about a third of the time, so a disagreement is worth less than 0."""
    answered = {name: f for name, f in finals.items() if f.get("type") != "abstain"}
    info = {"answered": sorted(answered)}
    if not answered:
        return {"type": "abstain"}, {**info, "reason": "no engine answered"}
    if len({_agree_key(f) for f in answered.values()}) > 1:
        return {"type": "abstain"}, {**info, "reason": "engines disagree"}
    name = next(n for n in finals if n in answered)  # first engine in order; equal answers but its own formatting
    return answered[name], {**info, "agree": len(answered) > 1, "from": name}
