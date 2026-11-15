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
   greedy decoding (temperature 0, seed 0). If the server dies, it restarts and the run resumes from the answers
   already written.
2. **Ingestion** (`src/datamate/ingest/`, deterministic Python, no model). Each business folder is read into
   cleaned tables:
   - header detection (title rows, 2-row headers, one sheet per month);
   - column roles found by README description, by name and by value shape (the model proposes roles for columns
     the rules leave open; each proposal must pass a value check);
   - Vietnamese dates and money parsed, unit conversion;
   - order header/line joins; blank cells of later rows of an order or block filled from its first row (rulebook §8).
   The tables become one DuckDB database per business, with canonical views (`sales_lines`, `orders`, debt,
   payments, imports, stock, lots...) and tested SQL macros for the train rulebook's definitions (`src/datamate/sql/`).
3. **Rulebook.** The split's `rulebook.md` is read at run time. Each tested macro is switched off when the rulebook
   no longer states its rule; the lines this rulebook adds or changes are shown to the model with each question
   that touches them, and route that question to the thinking tier.
4. **Shop audit** (`src/datamate/audit.py`, no model). Cross-checks of each shop's data that need no answer key:
   status words in a note column while the sales have no cancelled order, a line discount not taken off the amount,
   a README sentence that names a known column the roles missed, order fields given on the first row only. A shop
   with a flag is `suspect`.
5. **Per question** (`src/datamate/agent/pipeline.py`):
   - rule-based normalization: Telex typos, slang, amounts, dates; a model rewrite only when words stay unexplained;
   - time hints from `calendar.csv`, including the lunar calendar;
   - retrieval of similar train questions as examples (train split only);
   - the model writes one DuckDB query (thinking off; thinking on for questions on changed rules, unfamiliar
     wording or a failed first answer), which runs on a read-only connection with the question's `as_of` date bound;
   - answer checks: expected type, period, sign and plausibility. A failed check triggers one repair round.
6. **Choosing the answer.** The final answer is formatted as the official type. When the model finds the
   question unanswerable or hedges, a second model call (judge) re-reads the evidence and confirms, retries,
   or abstains. Deterministic gates (`src/datamate/agent/gates.py`) then abstain on answers measured unreliable on
   train (a reply cut by the token budget, a count the size of a money amount...), and on a `suspect` shop keep only
   date, boolean and unanswerable answers.

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
- Concurrency: the shops are split into 12 processes of 10 questions in flight each; the order of the answers in
  `submission.jsonl` may differ between runs.
- Time: questions not started 5 h 35 min after `predict.sh` starts are left unanswered (abstain); from 4 h 35 min
  no question takes the thinking tier.
- Shared model loading counts toward the 6 hours; the server start time is in `/result/predict.log`.

## Layout

```
/code/predict.sh              entry point
/code/README.md               this file
/code/requirements.txt        pinned inference dependencies (from uv.lock)
/code/pyproject.toml, uv.lock project and lock file; the environment is /code/.venv
/code/src/datamate/           inference code (ingest, sql, agent, preprocess, postprocess)
/code/assets/                 Telex lexicon, train README description -> role table, train-only dev id lists
/code/tests/                  unit tests (uv run pytest -q)
/code/models/Qwen3.5-9B/      model weights
/code/training_code/          README, train data, data_manifest.json
```
