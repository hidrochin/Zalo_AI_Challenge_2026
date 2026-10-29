"""Local re-implementation of the official scoring (Penalized Accuracy)."""

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from datamate.text import norm_key


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num_eq(p, r) -> bool:
    return _is_num(p) and _is_num(r) and math.isclose(p, r, rel_tol=1e-9, abs_tol=1e-6)


def _str_eq(p, forms) -> bool:
    return isinstance(p, str) and norm_key(p) in {norm_key(f) for f in forms if f is not None}


def _forms(ref, alias) -> list:
    out = [ref]
    if isinstance(alias, list):
        out += [a for a in alias if isinstance(a, str)]
    elif isinstance(alias, str):
        out.append(alias)
    return out


def _cell_eq(p, r, alias=None) -> bool:
    if isinstance(r, str):
        return _str_eq(p, _forms(r, alias))
    if _is_num(r):
        return _num_eq(p, r)
    return p == r


def _table_eq(p, r, alias) -> bool:
    if not isinstance(p, dict) or not isinstance(p.get("rows"), list) or not isinstance(p.get("columns"), list):
        return False
    rc, pc = [norm_key(c) for c in r["columns"]], [norm_key(c) for c in p["columns"]]
    if len(rc) != len(pc):
        return False
    order = [pc.index(c) for c in rc] if sorted(rc) == sorted(pc) and len(set(pc)) == len(pc) else list(range(len(rc)))
    prows = [[row[i] if i < len(row) else None for i in order] for row in p["rows"] if isinstance(row, list)]
    if len(prows) != len(r["rows"]):
        return False
    used = [False] * len(prows)
    for i, rrow in enumerate(r["rows"]):
        ral = alias[i] if isinstance(alias, list) and i < len(alias) else None
        hit = next(
            (j for j, prow in enumerate(prows) if not used[j]
             and all(_cell_eq(pv, rv, ral if k == 0 else None) for k, (pv, rv) in enumerate(zip(prow, rrow)))),
            None,
        )
        if hit is None:
            return False
        used[hit] = True
    return True


def is_correct(pred: dict, ref: dict) -> bool:
    if pred.get("type") != ref["type"]:
        return False
    t, p, r, alias = ref["type"], pred.get("answer"), ref.get("answer"), ref.get("alias")
    if t == "unanswerable":
        return True
    if t == "number":
        return _num_eq(p, r)
    if t == "percent":
        return _is_num(p) and math.isclose(round(p, 2), round(r, 2), abs_tol=1e-9)
    if t == "string":
        return _str_eq(p, _forms(r, alias))
    if t in ("date",):
        return isinstance(p, str) and p == r
    if t == "boolean":
        return isinstance(p, bool) and p == r
    if t == "list":
        if not isinstance(p, list) or len(p) != len(r):
            return False
        return all(_cell_eq(pv, rv, alias[i] if isinstance(alias, list) and i < len(alias) else None)
                   for i, (pv, rv) in enumerate(zip(p, r)))
    if t == "table":
        return _table_eq(p, r, alias)
    return False


def score_one(pred: dict | None, ref: dict) -> int:
    if pred is None or pred.get("type") in (None, "abstain"):
        return 0
    return 1 if is_correct(pred, ref) else -1


def read_jsonl(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def score(preds: dict[str, dict], refs: dict[str, dict], meta: dict[str, dict] | None = None, ids=None) -> dict:
    """PA over `ids` (default: all reference ids). meta[id] may carry 'business_id' for breakdowns."""
    ids = list(ids) if ids is not None else list(refs)
    rows = []
    for i in ids:
        s = score_one(preds.get(i), refs[i])
        rows.append((i, s))
    out = {"n": len(rows), **_summ([s for _, s in rows])}
    by_type, by_biz = defaultdict(list), defaultdict(list)
    for i, s in rows:
        by_type[refs[i]["type"]].append(s)
        if meta and i in meta:
            by_biz[meta[i].get("business_id")].append(s)
    out["by_type"] = {k: _summ(v) for k, v in sorted(by_type.items())}
    if by_biz:
        out["by_business"] = {k: _summ(v) for k, v in sorted(by_biz.items())}
    out["per_question"] = {i: s for i, s in rows}
    return out


def _summ(scores: list[int]) -> dict:
    c = Counter(scores)
    n = len(scores) or 1
    answered = c[1] + c[-1]
    return {
        "n": len(scores), "PA": round(sum(scores) / n, 4), "correct": c[1], "wrong": c[-1], "abstain": c[0],
        "coverage": round(answered / n, 4), "precision": round(c[1] / answered, 4) if answered else None,
    }
