# DataMate: offline text-to-SQL agent (Zalo AI Challenge 2026)

Answers everyday Vietnamese questions about a small shop's raw files (CSV / XLSX / SQLite) with a typed answer, an
`unanswerable` verdict, or an abstention. Everything runs offline inside this image.

## Run

```bash
bash /code/predict.sh        # no arguments; reads /data, writes /result
```

- `/result/submission.jsonl`: one line per question, written as soon as the question is answered.
- `/result/logs/<id>.json`: one record per question (below).
- `/result/predict.log` and `/result/vllm.log`: the run and model-server logs.
- `/result/config.json` and `/result/summary.json`: the settings and the totals.

Temporary files go to `/tmp/datamate`. `/data` is only read.

## Approach

1. **Model server.** `predict.sh` starts vLLM on the packaged weights: tensor parallel over the 2 GPUs, bf16,
   greedy decoding (temperature 0, seed 0), thinking on. If the server dies, it restarts and the run resumes
   from the answers already written.
2. **Ingestion** (`src/datamate/ingest/`, deterministic Python, no model). Each business folder is read into
   cleaned tables:
   - header detection (title rows, 2-row headers, one sheet per month);
   - column roles found by name and by value shape;
   - Vietnamese dates and money parsed, unit conversion;
   - order header/line joins, forward-fill of continuation rows.
   The tables become one DuckDB database per business, with canonical views (`sales_lines`, `orders`, debt,
   payments, imports, stock, lots...) and SQL macros (`src/datamate/sql/`).
3. **Rulebook.** The split's `rulebook.md` is read at run time. The model compiles its definitions into SQL
   macros, which are checked by running them on every store (`src/datamate/sql/compile.py`). It also writes shop
   views from each business README (`sql/shopviews.py`).
4. **Per question** (`src/datamate/agent/pipeline.py`):
   - rule-based normalization: Telex typos, slang, amounts, dates; then one model rewrite;
   - time hints from `calendar.csv`, including the lunar calendar;
   - retrieval of similar train questions as examples (train split only);
   - the model writes one DuckDB query, which runs on a read-only connection with the question's `as_of` date bound;
   - answer checks: expected type, period, sign and plausibility. A failed check triggers one repair round.
5. **Choosing the answer.** The final answer is formatted as the official type. When the model finds the
   question unanswerable or hedges, a second model call (judge) re-reads the evidence and confirms, retries,
   or abstains. If the checks still disagree after the repair, the system abstains.

No answer or logic is keyed on question or business IDs.

## Models

| model | source | revision | released | licence | parameters |
|---|---|---|---|---|---|
| Qwen3.5-9B | https://huggingface.co/Qwen/Qwen3.5-9B | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` | 2026-02-27 | Apache-2.0 | 9.65 B |

This is the only model used at inference (9.65 B of the 15 B allowed). There is no embedder, reranker or
classifier: example retrieval is TF-IDF. Weights are in `/code/models/Qwen3.5-9B`, unmodified (no fine-tuning).

## Data

- Only the provided **train** split is used: 30 businesses, 3000 questions with answers. It is in
  `/code/training_code/data/train`.
- At run time, the train questions and answers serve as retrieved examples.
- The Telex lexicon `assets/vi_rhymes.txt` was built from train text (`datamate build-lexicon`).
- No public-test or private-test data or answers were used to train, tune examples, or build any asset.
- Nothing was trained or generated. See `/code/training_code/README.md`.

## Per-question log (`/result/logs/<id>.json`)

Required fields first, then the full trace:

| field | content |
|---|---|
| `id` | the question id |
| `answer` | the final answer object, as in `submission.jsonl` |
| `elapsed_ms` | wall clock from when the question starts to its final answer (preprocessing, model calls, SQL) |
| `thinking` | the model's reasoning (`reasoning_content`), untruncated, one string per model call in call order (`""` for a call without reasoning); `null` if no call produced any |
| `code` | every SQL query that was executed, in order |
| `output` | the result of each query (or its error), in the same order |

The rest of the record holds the preprocessing, hints, the examples used, each model reply and the judge's verdict.

## Reproducibility and time

- Decoding: greedy (temperature 0), fixed seed 0, `PYTHONHASHSEED=0`.
- Concurrency: 16 questions in flight; the order of the answers in `submission.jsonl` may differ between runs.
- Shared model loading counts toward the 6 hours; the server start time is in `/result/predict.log`.

## Layout

```
/code/predict.sh              entry point
/code/README.md               this file
/code/requirements.txt        pinned inference dependencies (from uv.lock)
/code/pyproject.toml, uv.lock project and lock file; the environment is /code/.venv
/code/src/datamate/           inference code (ingest, sql, agent, preprocess, postprocess)
/code/assets/                 Telex lexicon, train-only dev id lists
/code/tests/                  unit tests (uv run pytest -q)
/code/models/Qwen3.5-9B/      model weights
/code/training_code/          README, train data, data_manifest.json
```
