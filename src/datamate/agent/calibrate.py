"""Calibrated confidence for the final decision (L8): p_correct of an answer from what the run saw while making it.

A logistic regression over features of the question's log (router tier and reasons, repairs, checks, the judge, which
macros the query called, how close the question is to train) is fitted on a train run's logs against the train gold
answers, leave-business-out, and saved as plain JSON (feature names, weights, threshold). At answer time an answer with
p_correct below the threshold abstains: under PA (+1 / 0 / -1) answering pays only when p_correct > 0.5.

The features must come from the same pipeline as the run they score: fit on a train run made with the flags of the
submission run, never on public or private logs (rule 5).
"""

import json
import math
import re
from pathlib import Path

import numpy as np

from datamate.eval.scorer import is_correct, read_jsonl

TYPES = ("number", "percent", "string", "date", "list", "table", "boolean", "unanswerable")
_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(", re.I)


def _final_sample(rec: dict) -> dict:
    return next((s for s in rec.get("samples") or [] if s.get("result") is not None), (rec.get("samples") or [{}])[0])


def features(rec: dict, tested: frozenset[str]) -> dict[str, float]:
    """Features of one question's log record. tested: tested_names(), whose calls count apart from other calls."""
    route = rec.get("route") or {}
    tier = route.get("tier", "none")
    reasons = " ".join(route.get("reasons") or [])
    s = _final_sample(rec)
    attempts = s.get("attempts") or []
    code = next((a.get("code") for a in reversed(attempts) if a.get("code")), "") or ""
    calls = {c.lower() for c in _CALL.findall(code)}
    judge = (rec.get("judge") or {}).get("action", "none")
    final = rec.get("final") or {}
    frame = rec.get("frame") or {}
    f = {
        "bias": 1.0,
        "near": float(rec.get("near") or 0.0),
        "tier_deep": float(tier == "deep"), "tier_escalated": float(tier == "escalated"),
        "why_changed_rule": float("changed rule" in reasons), "why_suspect": float("suspect" in reasons),
        "why_unfamiliar": float("unfamiliar" in reasons), "why_check": float("check" in reasons),
        "why_hedge": float("hedge" in reasons), "why_unanswerable": float("unanswerable" in reasons),
        "why_abstain": float("abstain" in reasons),
        "tiers_agree": float(route.get("agree") is True), "tiers_disagree": float(route.get("agree") is False),
        "attempts": float(len(attempts)),
        "had_error": float(any(a.get("error") for a in attempts)),
        "had_warning": float(any(a.get("warning") for a in attempts)),
        "contract": float(any(a.get("contract") for a in attempts)),
        "length_cut": float(any(a.get("finish_reason") == "length" for a in attempts)),
        "calls_tested": float(bool(calls & tested)), "reads_raw": float("raw_" in code),
        "judge_keep": float(judge == "keep"), "judge_changed": float(judge in ("answer", "unanswerable")),
        "expect_known": float((rec.get("expect") or {}).get("type") is not None),
        "fast": float(bool(rec.get("fast"))),
        "cap_suspect": float((frame.get("capability") or [None, None])[1] == "suspect"),
        "cap_absent": float((frame.get("capability") or [None, None])[1] == "absent"),
        "residual": float(min(len(frame.get("residual") or []), 5)),
        "has_metric": float(bool(frame.get("metric"))),
        "inconsistent": float(bool(rec.get("consistency"))),
        "magnitude_over": float((rec.get("magnitude") or 0) > 1),
    }
    for t in TYPES:
        f[f"type_{t}"] = float(final.get("type") == t)
    return f


def p_correct(model: dict, feats: dict[str, float]) -> float:
    z = sum(w * feats.get(n, 0.0) for n, w in zip(model["features"], model["weights"]))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _rows(run_dirs: list[Path], split_dir: Path) -> tuple[list[dict], list[str], list[int]]:
    """Features, business and label (1 right, 0 wrong) of every answered question of the runs (abstains carry no label)."""
    refs = {r["id"]: r for r in read_jsonl(Path(split_dir) / "answers.jsonl")}
    X, biz, y = [], [], []
    tested = tested_names()
    for rd in run_dirs:
        for log in sorted((Path(rd) / "logs").glob("*.json")):
            rec = json.loads(log.read_text(encoding="utf-8"))
            final = rec.get("calibrated_from") or rec.get("final") or {}
            ref = refs.get(rec["id"])
            if ref is None or final.get("type", "abstain") == "abstain":
                continue
            X.append(features({**rec, "final": final}, tested))
            biz.append(rec["business_id"])
            y.append(int(is_correct(final, ref)))
    return X, biz, y


def tested_names() -> frozenset[str]:
    """The hand-written rule macros of the SQL store (a query can call one only where the run offers it)."""
    from datamate.sql.agent import MACROS, PRIMITIVES

    return frozenset(m for m in MACROS if m not in PRIMITIVES)


def _fit(X: np.ndarray, y: np.ndarray, c: float):
    from sklearn.linear_model import LogisticRegression

    if len(set(y.tolist())) < 2:  # one class only: a constant model
        return None, float(y.mean() if len(y) else 0.5)
    m = LogisticRegression(C=c, max_iter=2000, fit_intercept=False)
    m.fit(X, y)
    return m, None


def calibrate(run_dirs: list[Path], split_dir: Path, out: Path, threshold: float = 0.5, c: float = 1.0) -> dict:
    """Leave-business-out report and the model fitted on every business, saved to `out` (JSON).
    The report gives PA over the answered questions with and without the threshold (abstains of the run add 0 to both)."""
    X, biz, y = _rows(run_dirs, split_dir)
    if not X:
        raise SystemExit("no answered train question with a gold answer in these runs")
    names = list(X[0])
    A = np.array([[x[n] for n in names] for x in X])
    Y = np.array(y)
    B = np.array(biz)
    p = np.zeros(len(Y))
    for b in sorted(set(biz)):  # leave-business-out
        test = B == b
        m, const = _fit(A[~test], Y[~test], c)
        p[test] = const if m is None else m.predict_proba(A[test])[:, 1]
    kept = p > threshold
    sign = 2 * Y - 1
    by_biz = {b: {"n": int((B == b).sum()), "before": int(sign[B == b].sum()), "after": int(sign[(B == b) & kept].sum())}
              for b in sorted(set(biz))}
    bins = []
    groups = {}
    for name, key in (("tier", lambda x: next((t for t in ("deep", "escalated") if x[f"tier_{t}"]), "normal/none")),
                      ("capability", lambda x: "suspect" if x["cap_suspect"] else "absent" if x["cap_absent"] else "ok/none"),
                      ("type", lambda x: next((t for t in TYPES if x[f"type_{t}"]), "?"))):
        keys = np.array([key(x) for x in X])
        groups[name] = {k: {"n": int((keys == k).sum()), "right": round(float(Y[keys == k].mean()), 3),
                            "before": int(sign[keys == k].sum()), "after": int(sign[(keys == k) & kept].sum())}
                        for k in sorted(set(keys))}
    for lo in np.arange(0.0, 1.0, 0.1):
        sel = (p >= lo) & (p < lo + 0.1 if lo < 0.9 else p <= 1.0)
        if sel.any():
            bins.append({"p": f"{lo:.1f}-{lo + 0.1:.1f}", "n": int(sel.sum()), "right": round(float(Y[sel].mean()), 3)})
    m, const = _fit(A, Y, c)
    weights = [const_logit(const)] + [0.0] * (len(names) - 1) if m is None else m.coef_[0].tolist()
    model = {"features": names, "weights": weights, "threshold": threshold, "n": len(Y), "right": int(Y.sum()),
             "runs": [str(r) for r in run_dirs]}
    report = {"answered": len(Y), "right": int(Y.sum()), "wrong": int(len(Y) - Y.sum()),
              "sum_before": int(sign.sum()), "sum_after_lobo": int(sign[kept].sum()),
              "dropped": int((~kept).sum()), "dropped_right": int(Y[~kept].sum()), "bins": bins, "by_business": by_biz, "groups": groups,
              "weights": dict(zip(names, [round(w, 3) for w in weights]))}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def const_logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def load(path: Path | None) -> dict | None:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).exists() else None
