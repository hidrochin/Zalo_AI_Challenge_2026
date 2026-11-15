# DataMate: Zalo AI Challenge 2026

An offline agent that answers everyday Vietnamese questions over a small business's CSV, XLSX and SQLite files, or
declines. Qwen3.5-9B (vLLM) writes one DuckDB query per question over a store built from the shop's files. A layer of
hand-written, train-tested macros and code checks wraps the model, and deterministic gates decide when to abstain.

This is the **final submission version**: code is frozen.

- **The pipeline, step by step (final, Vietnamese):** [docs/PIPELINE.md](docs/PIPELINE.md). Also holds the results, the
  checks behind every change, compliance and the known residual errors.
- **Task rules and data analysis:** [CLAUDE.md](CLAUDE.md).
- **Measurements per layer, history of decisions (Vietnamese):** [docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md).
- **Earlier plans, kept for reference:** [docs/PLAN_TEXT2SQL.md](docs/PLAN_TEXT2SQL.md),
  [docs/PROPOSAL_V3.md](docs/PROPOSAL_V3.md), [docs/PROPOSAL.md](docs/PROPOSAL.md).
- **Day-by-day status notes up to 2026-11-15:** git history of this file (`git show f42295a:README.md`).

## Results (public test, local scorer)

| run | what changed | PA |
|---|---|---|
| public_d | before slots | 0.595 |
| public_e | typed slots 0–3 | 0.716 (leaderboard 0.724) |
| public_g | `shop_suspect` gate by scope, repair round after a cut thinking reply, schema hints on Binder/Catalog errors | 0.775 |
| public_i | + `customer_group` fix, `unit_factor` slot, thinking budget 16384 | 0.789 |
| **final** | public_i with `unit_gap` no longer a suspect flag (gates replayed on public_i's logs) | **0.799** |

Train is unchanged by the final changes: `sql-check` 826/859, `facts-check` no misses, no slot fires and no audit flag
on any train shop. All 411 unit tests pass.

## Architecture

```
data/<biz>/ files + README ─► ingest ─► LLM: column roles, statuses ─► slots (LLM fills a narrow JSON, code applies)
                          ─► sales_lines, facts, audit (flags + scope) ─► DuckDB store + macros + schema card
question ─► rule preprocessing (+ LLM rewrite if words stay unexplained) ─► frame, time hints, examples, rulebook lines
         ─► router (thinking off / on) ─► Qwen writes SQL ─► run on DuckDB, lints, checks, one repair
         ─► escalate / judge ─► gates ─► submission.jsonl + logs/<id>.json
```

Details: [docs/PIPELINE.md](docs/PIPELINE.md).

## Quickstart

```bash
uv sync
uv run pytest -q                                     # unit tests
uv run datamate ingest --split train                 # cache tables, write schema cards to outputs/ingest/

# a run against an OpenAI-compatible server (vLLM) serving the model as `llm`: the submission flags
uv run datamate run --split public_test --backend local --base-url http://localhost:18000/v1 --model llm --engine sql \
    --thinking route --max-tokens 2048 --think-max-tokens 16384 --catalog delta --judge unans --context rich \
    --frame --rewrite residual --out outputs/<run>

uv run datamate score outputs/<run>                  # PA on train (split with answers.jsonl)
uv run datamate compare outputs/<a> outputs/<b>      # paired ΔPA, transitions
uv run datamate gate outputs/<run> --gold <answers.jsonl>   # replay the gates
uv run datamate slots --split <split> [--dry-run]    # the slots of each shop (triggers only with --dry-run)
uv run datamate audit --split <split>                # audit flags and scopes
uv run datamate sql-check                            # SQL macros against train gold (no LLM)
uv run datamate facts-check                          # fact tables against train gold (no LLM)
uv run datamate slots --split train --mutate all ... # slots on mutated train shops, contracts against gold
```

A run directory holds `submission.jsonl`, `logs/<id>.json`, `config.json`, `audit.json`, `slots.json`,
`capabilities.json`, and on train `report.json` and `summary.json`. Runs resume by default.

Version bumps:
- `INGEST_VERSION` (`ingest/business.py`) when ingestion output changes;
- `STORE_VERSION` (`sql/store.py`) when a view, macro, dimension or fact changes, then re-run `sql-check`;
- `dm.SEMANTIC_VERSION` when a `dm` primitive changes.

## Experiments on a rented GPU

The rented box only serves vLLM and runs experiments: never Docker or images there. `ground_truth.jsonl` (public
labels, measurement only) never leaves the Mac.

```bash
remote/setup_server.sh <port> <host> check|sync|serve|all   # GPU info; rsync code (+ data unless SKIP_DATA=1); vLLM in tmux
MAX_LEN=49152 remote/setup_server.sh <port> <host> serve    # restart vLLM with another context length (--replace)
remote/run_parallel.sh public_test outputs/rented/<run> 4 12 <datamate run flags>   # on the box: shops in 4 groups
```

## Docker submission

- **Image:** `zaic2026_datamate_oom:final`.
- **Entry point:** [docker/predict.sh](docker/predict.sh) reads `/data` and writes `/result/submission.jsonl` and
  `/result/logs/`.
- **Base:** `vllm/vllm-openai:v0.32.0-cu129`, minus torchcodec.
- **Inside the image:** the model weights, the train data and the code with its `.venv`.

Images so far:
1. **Image #1** (`2992a15d…`) failed the organizers' "Test Solution". The cause was bash's `GROUPS` and torchcodec cu130.
2. **Image #2** (`01f6c076…`, code `f42295a`) fixes both. It was built on the Mac on top of #1 with
   `docker/build_update.sh build|smoke|export`.
3. **The final image** comes from the final commit, built the same way. `build_update.sh` archives `src` from HEAD.
   Before the build, [docker/predict.sh](docker/predict.sh) must carry the final flags:
   - `--think-max-tokens 16384` in `RUN_ARGS`;
   - `--max-model-len 49152` in `SERVE_ARGS`.

## Compliance guardrails

- The `claude` and `gemini` backends refuse any split but `train`. Private test data never goes to an external API.
- Examples are retrieved only from train, and questions of the same business are excluded.
- There is no logic keyed on business or question ids.
- `/data` is read-only; caches and outputs live in `cache/` and `outputs/`.
- Nothing learned from the public test lives in inference code (organizers' QnA, `description/QnA.txt`): no public
  rulebook wording, macro, header, status word or layout, and no fix chosen on public labels, unless train has the case.
  - Question preprocessing is exempt, by the user's choice.
  - Changes accepted by the user after a public measurement are listed in docs/PIPELINE.md section 8, with their train
    checks.
