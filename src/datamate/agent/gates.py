"""Per-question decision gates (L8): deterministic rules on a finished question log that turn an answer into abstain,
or fix its shape. Each gate was measured on the train route run (3000 questions) before it was switched on; its words
come from train questions. `measure` replays them on any run with gold answers (`datamate gate`).
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from datamate.postprocess import _MONEY_WORD
from datamate.text import fold

# "bao nhiêu đơn / khách / ly ...": a count, not money
_COUNT = re.compile(r"\b(bao nhieu|bn|may|tong cong|tong so)\s+(don|don hang|hoa don|dia|bo|cai|mat hang|mon|san pham|khach|"
                    r"lan|phieu|chuyen|ly|goi|chai|hop|vien|lon|tuyp|vi|binh|bao|thung)\b")
_ITEMS = re.compile(r"\b(bao nhieu|bn|may|tong cong|tong so)\s+(mat hang|mon|san pham|sp|loai)\b")
_NAMES_ASKED = re.compile(r"\b(nhung|liet ke|ten|top|la gi|gom)\b")
_TABLE_WORDS = re.compile(r"\b(kem|bang|tung|moi nguoi|top)\b")
_UNANS_LINE = re.compile(r"^\s*UNANSWERABLE", re.M)


def _attempts(rec: dict) -> list[dict]:
    return [a for s in rec.get("samples") or [] for a in s.get("attempts") or []]


# Answer types a constant per-shop bias of the store turns wrong (every amount off by a few percent); a best day, the
# direction of a comparison and "the data has no such field" do not change under such a bias
BIASED_TYPES = ("number", "percent", "string", "list", "table")


def gate(rec: dict, shop: str = "clean") -> tuple[dict, str] | None:
    """(new final, gate name) when a gate changes the decided answer of this question log, else None.
    shop: the shop's audit level (audit.py); a suspect shop keeps only the answer types a store bias does not change."""
    final = rec.get("final") or {}
    t, a = final.get("type", "abstain"), final.get("answer")
    if shop == "suspect" and t in BIASED_TYPES:
        return {"type": "abstain"}, "shop_suspect"
    text = fold((rec.get("pre") or {}).get("rules") or rec.get("question") or "")
    expect = rec.get("expect") or {}
    atts = _attempts(rec)
    if t == "abstain":
        # the model put its UNANSWERABLE verdict inside the SQL block, so the block failed (train: 3/3 gold unanswerable)
        last = atts[-1] if atts else {}
        if _UNANS_LINE.search(last.get("code") or "") and (rec.get("judge") or {}).get("action") != "abstain":
            return {"type": "unanswerable", "reason": "UNANSWERABLE"}, "unans_in_sql"
        return None
    # no gate on a thinking reply cut at the budget: the later attempt without thinking is right on train 16 / 2
    if t == "number" and isinstance(a, (int, float)) and abs(a) >= 1e6 and _COUNT.search(text) \
            and not _MONEY_WORD.search(text):
        return {"type": "abstain"}, "count_money"  # a count the size of a money amount (train 4/4 wrong)
    if t == "list" and isinstance(a, list) and _ITEMS.search(text) and not _NAMES_ASKED.search(text):
        return {"type": "number", "answer": len(a)}, "items_len"  # "bao nhiêu món" answered by the list of them
    if t == "table" and not _TABLE_WORDS.search(text):
        return {"type": "abstain"}, "table_words"  # train 5/5 wrong
    if expect.get("hard") and expect.get("type") and t not in (expect["type"], "unanswerable"):
        return {"type": "abstain"}, "expect_type"
    return None


def apply(rec: dict, shop: str = "clean") -> dict:
    """The log with its gate applied in place: `final` replaced, `gated` = {gate, from}."""
    got = gate(rec, shop)
    if got is not None:
        rec["gated"] = {"gate": got[1], "from": rec["final"]}
        rec["final"] = got[0]
    return rec


def measure(run: Path, gold_path: Path, shops: dict[str, str] | None = None) -> dict:
    """Replay the gates on a run's logs: per gate, outcome before -> after against gold (right / wrong / abstain), and
    the PA change. A log that already went through a gate is replayed from its pre-gate answer.
    shops: business id -> audit level (default: the run's audit.json, else every shop clean)."""
    if shops is None:
        path = Path(run, "audit.json")
        shops = {b: a["level"] for b, a in json.loads(path.read_text(encoding="utf-8")).items()} if path.exists() else {}
    from datamate.eval.scorer import is_correct, read_jsonl

    gold = {r["id"]: r for r in read_jsonl(gold_path)}

    def outcome(final: dict, ref: dict) -> str:
        return "abstain" if final.get("type", "abstain") == "abstain" else "right" if is_correct(final, ref) else "wrong"

    moves: dict[str, Counter] = defaultdict(Counter)
    n = before = after = 0
    for log in sorted(Path(run, "logs").glob("*.json")):
        rec = json.loads(log.read_text(encoding="utf-8"))
        ref = gold.get(rec["id"])
        if ref is None:
            continue
        if rec.get("gated"):
            rec["final"] = rec.pop("gated")["from"]
        n += 1
        o = outcome(rec["final"], ref)
        got = gate(rec, shops.get(rec["business_id"], "clean"))
        o2 =outcome(got[0], ref) if got else o
        if got:
            moves[got[1]][f"{o}->{o2}"] += 1
        score = {"right": 1, "wrong": -1, "abstain": 0}
        before += score[o]
        after += score[o2]
    return {"n": n, "PA_before": round(before / max(n, 1), 4), "PA_after": round(after / max(n, 1), 4),
            "gates": {g: dict(c) for g, c in sorted(moves.items())}}
