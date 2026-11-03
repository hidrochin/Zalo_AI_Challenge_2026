"""Run diagnostics: why a question abstained, and paired comparison of two runs on the same questions.

Outcomes split abstain by cause, so a PA gain from abstaining more is visible:
  correct / incorrect / exec_failure (no program ran to an answer) / validation (a check rejected the
  answer) / vote (samples disagreed, weak unanswerable) / format (finalize rejected a malformed answer).
"""

import json
import random
from collections import Counter
from pathlib import Path


def abstain_cause(rec: dict) -> str:
    vote = rec.get("vote") or {}
    if vote.get("valid"):
        return "vote" if vote.get("reason") else "format"
    errors = [s.get("error") or "" for s in rec.get("samples", [])]
    return "validation" if errors and all(e.startswith("unconfirmed") for e in errors) else "exec_failure"


def outcomes(run_dir: Path, per_question: dict[str, int]) -> dict:
    """Counts of correct / incorrect / abstain-by-cause over the scored questions of a run."""
    c: Counter = Counter()
    for i, s in per_question.items():
        if s:
            c["correct" if s > 0 else "incorrect"] += 1
            continue
        log = Path(run_dir) / "logs" / f"{i}.json"
        c[abstain_cause(json.loads(log.read_text(encoding="utf-8"))) if log.exists() else "missing"] += 1
    return dict(c)


def compare(a_dir: Path, b_dir: Path, n_boot: int = 2000, seed: int = 0) -> dict:
    """Paired comparison on the questions both runs scored: transitions and a bootstrap 95% CI of ΔPA (b - a)."""
    a = json.loads((Path(a_dir) / "report.json").read_text(encoding="utf-8"))["per_question"]
    b = json.loads((Path(b_dir) / "report.json").read_text(encoding="utf-8"))["per_question"]
    ids = sorted(set(a) & set(b))
    name = {1: "correct", 0: "abstain", -1: "wrong"}
    trans = Counter(f"{name[a[i]]} -> {name[b[i]]}" for i in ids if a[i] != b[i])
    d = [b[i] - a[i] for i in ids]
    rng = random.Random(seed)
    boots = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(n_boot)) if d else [0.0]
    pa = lambda r: round(sum(r[i] for i in ids) / max(len(ids), 1), 4)  # noqa: E731
    return {
        "n": len(ids), "PA_a": pa(a), "PA_b": pa(b), "delta": round(sum(d) / max(len(d), 1), 4),
        "ci95": [round(boots[int(0.025 * len(boots))], 4), round(boots[int(0.975 * len(boots)) - 1], 4)],
        "transitions": dict(trans.most_common()),
        "outcomes_a": outcomes(a_dir, {i: a[i] for i in ids}), "outcomes_b": outcomes(b_dir, {i: b[i] for i in ids}),
    }
