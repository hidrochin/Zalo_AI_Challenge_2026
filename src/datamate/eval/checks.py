"""Measure answer checks offline: false alarms on train gold answers, and recall on wrong answers of logged runs.

Under PA a check that fires on a correct answer costs a point (it abstains), so a check is only used when it
never fires on gold.
"""

import json
from collections import Counter
from pathlib import Path

from datamate.agent.hints import time_hints
from datamate.eval.scorer import read_jsonl
from datamate.ingest import load_calendar
from datamate.postprocess import check_answer, date_window, expect_answer, invariants
from datamate.preprocess import rules_normalize


def _flags(result: dict, question: str, as_of: str, cal) -> list[str]:
    text = rules_normalize(question).text
    e = expect_answer(text)
    e.window = date_window(text, time_hints(text, as_of, cal))
    msg, hard = check_answer(result, e)
    out = [n for n, _ in invariants(result, text, e.window)]
    return out + (["expect:" + ("hard" if hard else "soft")] if msg else [])


def evaluate(data_root: Path, run_dirs: list[Path]) -> dict:
    train = Path(data_root) / "train"
    cal = load_calendar(train)
    qs = {q["id"]: q for q in read_jsonl(train / "questions.jsonl")}
    gold = {a["id"]: a for a in read_jsonl(train / "answers.jsonl")}
    false_alarm, examples = Counter(), {}
    for i, g in gold.items():
        if g["type"] == "unanswerable":
            continue
        for n in _flags({"type": g["type"], "answer": g["answer"]}, qs[i]["question"], qs[i]["as_of_date"], cal):
            false_alarm[n] += 1
            examples.setdefault(n, (qs[i]["question"], g["answer"]))
    caught, wrong, seen = Counter(), 0, set()
    for rd in run_dirs:
        rep = json.loads((Path(rd) / "report.json").read_text(encoding="utf-8"))["per_question"]
        for i, s in rep.items():
            log = Path(rd) / "logs" / f"{i}.json"
            if s != -1 or not log.exists():
                continue
            final = json.loads(log.read_text(encoding="utf-8"))["final"]
            key = (i, json.dumps(final, sort_keys=True, ensure_ascii=False))
            if key in seen:  # the same wrong answer in several runs counts once
                continue
            seen.add(key)
            wrong += 1
            for n in set(_flags(final, qs[i]["question"], qs[i]["as_of_date"], cal)):
                caught[n] += 1
    return {"gold_answers": sum(g["type"] != "unanswerable" for g in gold.values()),
            "false_alarms_on_gold": dict(false_alarm), "false_alarm_examples": {k: str(v)[:160] for k, v in examples.items()},
            "distinct_wrong_answers": wrong, "caught_wrong": dict(caught)}
