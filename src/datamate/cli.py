"""Command line: `datamate ingest | run | score | export-examples`."""

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path


def _ingest_one(args):
    split_dir, biz_id, cache_dir, rebuild, card_dir = args
    from datamate.ingest import load_business
    from datamate.ingest.schema_card import schema_card

    t0 = time.time()
    try:
        biz = load_business(Path(split_dir), biz_id, Path(cache_dir), rebuild=rebuild)
    except Exception as e:
        return {"business": biz_id, "error": repr(e)}
    card = schema_card(biz)
    (Path(card_dir) / f"{biz_id}.md").write_text(card, encoding="utf-8")
    return {
        "business": biz_id, "seconds": round(time.time() - t0, 1), "tables": len(biz.tables),
        "rows": sum(len(d) for d in biz.tables.values()), "warnings": biz.warnings, "card_chars": len(card),
    }


def cmd_ingest(a) -> None:
    split_dir = Path(a.data_root) / a.split
    bizs = a.biz or sorted(p.name for p in (split_dir / "data").iterdir() if p.is_dir())
    card_dir = Path(a.out) / a.split
    card_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(str(split_dir), b, a.cache_dir, a.rebuild, str(card_dir)) for b in bizs]
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(_ingest_one, jobs))
    print(f"{'business':10} {'sec':>6} {'tables':>6} {'rows':>9} {'card':>7}  warnings")
    for r in rows:
        if "error" in r:
            print(f"{r['business']:10} ERROR {r['error']}")
            continue
        print(f"{r['business']:10} {r['seconds']:6.1f} {r['tables']:6d} {r['rows']:9d} {r['card_chars']:7d}  {'; '.join(r['warnings'])[:120]}")
    (card_dir / "_summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nschema cards written to {card_dir}/")


def _print_score(s: dict) -> None:
    prec = "n/a" if s["precision"] is None else f"{s['precision']:.2%}"
    print(f"\nPA={s['PA']:.4f}  n={s['n']}  correct={s['correct']} wrong={s['wrong']} abstain={s['abstain']}"
          f"  coverage={s['coverage']:.2%}  precision={prec}")
    if "outcomes" in s:
        print("outcomes:", ", ".join(f"{k}={v}" for k, v in sorted(s["outcomes"].items())))
    for title in ("by_type", "by_business"):
        if title in s:
            print(f"\n{title}:")
            for k, v in s[title].items():
                print(f"  {str(k):14} n={v['n']:4d} PA={v['PA']:+.3f} correct={v['correct']:4d} wrong={v['wrong']:4d} abstain={v['abstain']:4d}")


def cmd_run(a) -> None:
    from datamate.agent.pipeline import LLMUnavailable, RunConfig, run

    out = a.out or f"outputs/{a.split}_{a.backend}_{time.strftime('%m%d_%H%M%S')}"
    think = {"on": True, "off": False, "default": None}[a.thinking]
    ids = list(a.ids or [])
    if a.ids_file:
        ids += [ln.strip() for ln in Path(a.ids_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    cfg = RunConfig(
        split=a.split, data_root=Path(a.data_root), out_dir=Path(out), cache_dir=Path(a.cache_dir),
        backend=a.backend, engine=a.engine, model=a.model, base_url=a.base_url, samples=a.samples, temperature=a.temperature,
        max_tokens=a.max_tokens, enable_thinking=think, effort=a.effort, concurrency=a.concurrency,
        exec_workers=a.exec_workers, timeout=a.timeout, repair=a.repair, k_examples=a.k_examples,
        examples_path=Path(a.examples) if a.examples else None, min_share=a.min_share, unans_share=a.unans_share,
        seed=a.seed, limit=a.limit, businesses=a.biz or [], ids=ids, resume=not a.no_resume,
        llm_retries=a.llm_retries, normalize=a.normalize, fast=a.fast, keep_tables=Path(a.keep_tables) if a.keep_tables else None,
        oracle=Path(a.oracle) if a.oracle else None, catalog=a.catalog, think_rewrite=a.think_rewrite,
        shop_views=a.shop_views, judge=a.judge, think_max_tokens=a.think_max_tokens, prepare_only=a.prepare_only,
    )
    try:
        summary = run(cfg)
    except LLMUnavailable as e:
        raise SystemExit(f"model server unavailable ({e}); run the same command again to resume") from e
    print(f"\nrun dir: {out}  ({summary['new']} new questions, {summary['seconds']}s)")
    if "score" in summary:
        _print_score(summary["score"])


def cmd_score(a) -> None:
    from datamate.agent.pipeline import score_run

    _print_score(score_run(Path(a.run), Path(a.data_root) / a.split))


def cmd_compare(a) -> None:
    from datamate.eval.compare import compare

    print(json.dumps(compare(Path(a.a), Path(a.b)), ensure_ascii=False, indent=1))


def cmd_eval_checks(a) -> None:
    from datamate.eval.checks import evaluate

    print(json.dumps(evaluate(Path(a.data_root), [Path(r) for r in a.runs]), ensure_ascii=False, indent=1))


def cmd_sql_build(a) -> None:
    from datamate.eval.scorer import read_jsonl
    from datamate.sql.store import ensure_store

    split_dir = Path(a.data_root) / a.split
    base = Path(a.cache_dir).parent
    for biz in sorted({q["business_id"] for q in read_jsonl(split_dir / "questions.jsonl")}):
        path = ensure_store(split_dir, biz, base, rebuild=a.rebuild)
        print(f"{biz}: {path} ({path.stat().st_size / 1e6:.1f} MB)")


def cmd_sql_check(a) -> None:
    from datamate.sql.check import run_sql_checks

    print(json.dumps(run_sql_checks(Path(a.data_root), Path(a.cache_dir).parent), ensure_ascii=False, indent=1))


def cmd_semantic_check(a) -> None:
    from datamate.eval.semantic import run_checks

    rep = run_checks(Path(a.data_root), Path(a.cache_dir), a.only)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_facts_check(a) -> None:
    from datamate.eval.factcheck import run_fact_checks

    print(json.dumps(run_fact_checks(Path(a.data_root), Path(a.cache_dir)), ensure_ascii=False, indent=1))


def cmd_make_dev(a) -> None:
    from collections import Counter

    from datamate.eval.devset import make_devset
    from datamate.eval.scorer import read_jsonl

    ids = make_devset(Path(a.data_root) / "train", n=a.n, seed=a.seed)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(ids) + "\n", encoding="utf-8")
    types = {r["id"]: r["type"] for r in read_jsonl(Path(a.data_root) / "train" / "answers.jsonl")}
    bizs = {q["id"]: q["business_id"] for q in read_jsonl(Path(a.data_root) / "train" / "questions.jsonl")}
    print(f"{len(ids)} ids -> {a.out}")
    print("types:", dict(Counter(types[i] for i in ids).most_common()))
    print("businesses:", len({bizs[i] for i in ids}))


def cmd_export(a) -> None:
    from datamate.agent.pipeline import export_examples

    n = export_examples([Path(r) for r in a.runs], Path(a.data_root) / "train", Path(a.out))
    print(f"added {n} verified programs to {a.out}")


def cmd_build_lexicon(a) -> None:
    from datamate.eval.scorer import read_jsonl
    from datamate.preprocess.telex import RHYMES_PATH, build_rhymes

    train = Path(a.data_root) / "train"  # train text only (rule 5)
    texts = [q["question"] for q in read_jsonl(train / "questions.jsonl")] + [(train / "rulebook.md").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in sorted((train / "data").glob("*/README.md"))]
    rhymes = build_rhymes(texts)
    RHYMES_PATH.write_text("\n".join(rhymes) + "\n", encoding="utf-8")
    print(f"{len(rhymes)} rhymes -> {RHYMES_PATH}")


def cmd_normalize(a) -> None:
    """Rule-based preprocessing of every question of a split (with each shop's names), for review."""
    import json

    from datamate.canon import build_sales
    from datamate.eval.scorer import read_jsonl
    from datamate.ingest import load_business
    from datamate.preprocess import EntityIndex, rules_normalize

    split_dir = Path(a.data_root) / a.split
    qs = [q for q in read_jsonl(split_dir / "questions.jsonl") if not a.biz or q["business_id"] in a.biz]
    out = Path(a.out or f"outputs/normalize/{a.split}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    index, cur, changed = None, None, 0
    with out.open("w", encoding="utf-8") as f:
        for q in sorted(qs, key=lambda q: q["business_id"]):
            if q["business_id"] != cur:
                cur = q["business_id"]
                biz = load_business(split_dir, cur, Path(a.cache_dir))
                roles = {n: ti.roles for n, ti in biz.info.items()}
                index = EntityIndex.from_tables(biz.tables, roles, build_sales(biz.tables, roles)[0])
            r = rules_normalize(q["question"], index)
            changed += r.text != q["question"]
            f.write(json.dumps({"id": q["id"], "business_id": cur, "question": q["question"], "rules": r.text,
                                "changes": r.changes, "amounts": r.amounts,
                                "mentions": [[m.kind, m.name, m.full] for m in r.mentions]}, ensure_ascii=False) + "\n")
    print(f"{len(qs)} questions, {changed} changed -> {out}")


def cmd_validate(a) -> None:
    import json

    from datamate.eval.scorer import read_jsonl
    from datamate.postprocess import validate_submission

    ids = [q["id"] for q in read_jsonl(Path(a.data_root) / a.split / "questions.jsonl")]
    rep = validate_submission(read_jsonl(Path(a.submission)), ids)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if rep["duplicates"] or rep["unknown_ids"] or rep["malformed"]:
        raise SystemExit(1)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="datamate")
    p.add_argument("--data-root", default="data")
    p.add_argument("--cache-dir", default="cache/ingest")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="load businesses, cache cleaned tables, write schema cards")
    s.add_argument("--split", default="train")
    s.add_argument("--biz", nargs="*")
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--out", default="outputs/ingest")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("run", help="answer questions and (on train) score them")
    s.add_argument("--split", default="train")
    s.add_argument("--backend", choices=["local", "claude", "gemini", "dummy"], default="local")
    s.add_argument("--engine", choices=["pandas", "sql"], default="pandas", help="what the model writes")
    s.add_argument("--model", default="qwen3.5:9b")
    s.add_argument("--base-url", default="http://localhost:11434/v1", help="OpenAI-compatible server (Ollama, vLLM, ...)")
    s.add_argument("--samples", type=int, default=1, help="1 greedy + (samples-1) seeded samples, then vote")
    s.add_argument("--temperature", type=float, default=0.6)
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--thinking", choices=["on", "off", "default"], default="default", help="Qwen3 chat-template switch (vLLM)")
    s.add_argument("--effort", default="medium", help="Claude effort level; Ollama reasoning_effort when --thinking on")
    s.add_argument("--concurrency", type=int, default=4)
    s.add_argument("--exec-workers", type=int, default=2)
    s.add_argument("--timeout", type=int, default=30)
    s.add_argument("--repair", type=int, default=1)
    s.add_argument("--k-examples", type=int, default=4)
    s.add_argument("--examples", default="assets/examples.jsonl")
    s.add_argument("--min-share", type=float, default=0.5)
    s.add_argument("--unans-share", type=float, default=0.6)
    s.add_argument("--llm-retries", type=int, default=6, help="retries on model-server errors before the run stops")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--limit", type=int)
    s.add_argument("--biz", nargs="*")
    s.add_argument("--ids", nargs="*")
    s.add_argument("--ids-file", help="file with one question id per line (e.g. assets/dev120.txt)")
    s.add_argument("--out")
    s.add_argument("--no-resume", action="store_true")
    s.add_argument("--normalize", choices=["off", "rules", "llm"], default="llm",
                   help="question preprocessing: rules only, or rules + one LLM rewrite")
    s.add_argument("--keep-tables", help="experiment: JSON of question id -> raw tables shown to the model (SQL engine)")
    s.add_argument("--oracle", help="experiment: JSON of notes per question and SQL setup per business (debug/oracle)")
    s.add_argument("--fast", action="store_true", help="answer questions a closed frame explains in full without the model")
    s.add_argument("--catalog", choices=["full", "primitives", "compiled"], default="full",
                   help="SQL: full = hand-written rulebook macros; primitives = generic helpers only; compiled = generic helpers "
                        "+ macros the model writes from the split's rulebook at the start of the run")
    s.add_argument("--shop-views", action="store_true", help="SQL: the model writes views per shop from its README and raw tables")
    s.add_argument("--judge", choices=["off", "unans", "all"], default="off",
                   help="second agent on UNANSWERABLE verdicts (unans) or on every verdict (all)")
    s.add_argument("--think-max-tokens", type=int, default=8192, help="token budget of the compile, shop-view and judge calls")
    s.add_argument("--prepare-only", action="store_true", help="run the rulebook compiler and shop views, answer nothing")
    s.add_argument("--think-rewrite", action="store_true", help="thinking also for the rewrite and ingest/rule questions")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("score", help="score a run directory against train answers")
    s.add_argument("run")
    s.add_argument("--split", default="train")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("compare", help="paired comparison of two scored runs (transitions, bootstrap CI of ΔPA)")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("eval-checks", help="answer checks: false alarms on train gold, recall on wrong answers of runs")
    s.add_argument("runs", nargs="*")
    s.set_defaults(func=cmd_eval_checks)

    s = sub.add_parser("semantic-check", help="contract tests of the semantic primitives against train gold")
    s.add_argument("--only", nargs="*")
    s.add_argument("--out")
    s.set_defaults(func=cmd_semantic_check)

    s = sub.add_parser("sql-build", help="build the per-business DuckDB databases of a split")
    s.add_argument("--split", default="train")
    s.add_argument("--rebuild", action="store_true")
    s.set_defaults(func=cmd_sql_build)

    s = sub.add_parser("sql-check", help="contract tests of the SQL macros against train gold")
    s.set_defaults(func=cmd_sql_check)

    s = sub.add_parser("facts-check", help="fact helpers against train gold: debt, trips, imports, profit, stock, lots (no LLM)")
    s.add_argument("--data-root", default="data")
    s.add_argument("--cache-dir", default="cache/ingest")
    s.set_defaults(func=cmd_facts_check)

    s = sub.add_parser("make-dev", help="write a stratified train subset (all types, all businesses)")
    s.add_argument("--n", type=int, default=120)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", default="assets/dev120.txt")
    s.set_defaults(func=cmd_make_dev)

    s = sub.add_parser("export-examples", help="add correct programs from train runs to the example bank")
    s.add_argument("runs", nargs="+")
    s.add_argument("--out", default="assets/examples.jsonl")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("build-lexicon", help="rebuild assets/vi_rhymes.txt (Telex decoder) from train text")
    s.set_defaults(func=cmd_build_lexicon)

    s = sub.add_parser("normalize", help="rule-based question preprocessing of a split, for review")
    s.add_argument("--split", default="train")
    s.add_argument("--biz", nargs="*")
    s.add_argument("--out")
    s.set_defaults(func=cmd_normalize)

    s = sub.add_parser("validate", help="check a submission file's format and ids")
    s.add_argument("submission")
    s.add_argument("--split", default="public_test")
    s.set_defaults(func=cmd_validate)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    sys.exit(main())
