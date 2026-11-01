"""Stratified train subset for fast iteration: every answer type and every business is covered."""

import random
from collections import Counter, defaultdict
from pathlib import Path

from datamate.eval.scorer import read_jsonl


def make_devset(train_dir: Path, n: int = 120, min_per_type: int = 4, seed: int = 0) -> list[str]:
    """Pick n question ids: per-type quota proportional to train (floor `min_per_type`, rare types
    over-sampled so unanswerable/table/boolean are measurable), spread round-robin over businesses."""
    qs = {q["id"]: q for q in read_jsonl(Path(train_dir) / "questions.jsonl")}
    types = {a["id"]: a["type"] for a in read_jsonl(Path(train_dir) / "answers.jsonl")}
    pools: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for qid in sorted(types):
        pools[types[qid]][qs[qid]["business_id"]].append(qid)

    counts = Counter(types.values())
    total = sum(counts.values())
    quota = {t: max(min_per_type, round(n * c / total)) for t, c in counts.items()}
    big = max(quota, key=quota.get)
    quota[big] -= sum(quota.values()) - n  # absorb rounding/floors in the largest type

    rng = random.Random(seed)
    used: Counter = Counter()
    picked: list[str] = []
    for t in sorted(quota, key=lambda t: counts[t]):  # rare types first, so they claim businesses freely
        by_biz = {b: rng.sample(ids, len(ids)) for b, ids in pools[t].items()}
        for _ in range(quota[t]):
            live = [b for b in by_biz if by_biz[b]]
            if not live:
                break
            b = min(live, key=lambda b: (used[b], rng.random()))
            picked.append(by_biz[b].pop())
            used[b] += 1
    return sorted(picked)
