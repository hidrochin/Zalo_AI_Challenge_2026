"""Contract tests of the SQL macros against train gold (no LLM): the eval/semantic.py questions, answered in SQL."""

from collections import defaultdict
from pathlib import Path

import duckdb

from datamate import dm
from datamate.agent.hints import time_hints
from datamate.eval.scorer import read_jsonl
from datamate.eval.semantic import find_check
from datamate.ingest import load_calendar
from datamate.preprocess import rules_normalize
from datamate.sql.store import connect, ensure_store, run_select, set_as_of


def _literal(ts) -> str:
    return f"date '{ts.date().isoformat()}'"


def run_sql_checks(data_root: Path, cache_dir: Path, max_misses: int = 8) -> dict:
    train = Path(data_root) / "train"
    cal = load_calendar(train)
    gold = {a["id"]: a for a in read_jsonl(train / "answers.jsonl")}
    todo = defaultdict(list)
    for q in read_jsonl(train / "questions.jsonl"):
        text = rules_normalize(q["question"]).text
        check = find_check(text, gold[q["id"]]["type"])
        if check is not None:
            todo[q["business_id"]].append((check, q, text))
    rep = defaultdict(lambda: {"matched": 0, "exact": 0, "skipped": defaultdict(int), "misses": []})
    for biz_id in sorted(todo):
        con = connect(ensure_store(train, biz_id, cache_dir), todo[biz_id][0][1]["as_of_date"])
        for check, q, text in todo[biz_id]:
            r = rep[check.name]
            r["matched"] += 1
            calls = [h.split(" -> ")[1].split(" = ")[0].split("  #")[0] for h in time_hints(text, q["as_of_date"], cal)]
            if len(calls) != 1:
                r["skipped"]["periods != 1"] += 1
                continue
            dm.set_context(q["as_of_date"], cal, None)
            p = eval(calls[0], {n: getattr(dm, n) for n in dm.__all__})
            set_as_of(con, q["as_of_date"])
            try:
                value = run_select(con, f"select {check.name}({_literal(p.start)}, {_literal(p.end)})")[1][0][0]
            except duckdb.Error as e:
                r["skipped"][type(e).__name__] += 1
                continue
            g = gold[q["id"]]["answer"]
            if value is not None and abs(float(value) - float(g)) < 1e-6:
                r["exact"] += 1
            elif len(r["misses"]) < max_misses:
                r["misses"].append(f"{biz_id} {q['id']} {q['question'][:70]!r} gold={g} got={value}")
        con.close()
    return {name: {**r, "skipped": dict(r["skipped"])} for name, r in rep.items()}
