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
    for title in ("by_type", "by_business"):
        if title in s:
            print(f"\n{title}:")
            for k, v in s[title].items():
                print(f"  {str(k):14} n={v['n']:4d} PA={v['PA']:+.3f} correct={v['correct']:4d} wrong={v['wrong']:4d} abstain={v['abstain']:4d}")


def cmd_run(a) -> None:
    from datamate.agent.pipeline import RunConfig, run

    out = a.out or f"outputs/{a.split}_{a.backend}_{time.strftime('%m%d_%H%M%S')}"
    think = {"on": True, "off": False, "default": None}[a.thinking]
    ids = list(a.ids or [])
    if a.ids_file:
        ids += [ln.strip() for ln in Path(a.ids_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    cfg = RunConfig(
        split=a.split, data_root=Path(a.data_root), out_dir=Path(out), cache_dir=Path(a.cache_dir),
        backend=a.backend, model=a.model, base_url=a.base_url, samples=a.samples, temperature=a.temperature,
        max_tokens=a.max_tokens, enable_thinking=think, effort=a.effort, concurrency=a.concurrency,
        exec_workers=a.exec_workers, timeout=a.timeout, repair=a.repair, k_examples=a.k_examples,
        examples_path=Path(a.examples) if a.examples else None, min_share=a.min_share, unans_share=a.unans_share,
        seed=a.seed, limit=a.limit, businesses=a.biz or [], ids=ids, resume=not a.no_resume,
    )
    summary = run(cfg)
    print(f"\nrun dir: {out}  ({summary['new']} new questions, {summary['seconds']}s)")
    if "score" in summary:
        _print_score(summary["score"])


def cmd_score(a) -> None:
    from datamate.agent.pipeline import score_run

    _print_score(score_run(Path(a.run), Path(a.data_root) / a.split))


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
    s.add_argument("--backend", choices=["local", "claude", "dummy"], default="local")
    s.add_argument("--model", default="qwen3.5:9b")
    s.add_argument("--base-url", default="http://localhost:11434/v1", help="OpenAI-compatible server (Ollama, vLLM, ...)")
    s.add_argument("--samples", type=int, default=1, help="1 greedy + (samples-1) seeded samples, then vote")
    s.add_argument("--temperature", type=float, default=0.6)
    s.add_argument("--max-tokens", type=int, default=2048)
    s.add_argument("--thinking", choices=["on", "off", "default"], default="default", help="Qwen3 chat-template switch (vLLM)")
    s.add_argument("--effort", default="medium", help="Claude effort level")
    s.add_argument("--concurrency", type=int, default=4)
    s.add_argument("--exec-workers", type=int, default=2)
    s.add_argument("--timeout", type=int, default=30)
    s.add_argument("--repair", type=int, default=1)
    s.add_argument("--k-examples", type=int, default=4)
    s.add_argument("--examples", default="assets/examples.jsonl")
    s.add_argument("--min-share", type=float, default=0.5)
    s.add_argument("--unans-share", type=float, default=0.6)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--limit", type=int)
    s.add_argument("--biz", nargs="*")
    s.add_argument("--ids", nargs="*")
    s.add_argument("--ids-file", help="file with one question id per line (e.g. assets/dev120.txt)")
    s.add_argument("--out")
    s.add_argument("--no-resume", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("score", help="score a run directory against train answers")
    s.add_argument("run")
    s.add_argument("--split", default="train")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("make-dev", help="write a stratified train subset (all types, all businesses)")
    s.add_argument("--n", type=int, default=120)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", default="assets/dev120.txt")
    s.set_defaults(func=cmd_make_dev)

    s = sub.add_parser("export-examples", help="add correct programs from train runs to the example bank")
    s.add_argument("runs", nargs="+")
    s.add_argument("--out", default="assets/examples.jsonl")
    s.set_defaults(func=cmd_export)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    sys.exit(main())
