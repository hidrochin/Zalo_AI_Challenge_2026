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
2. **Ingestion** (`src/datamate/ingest/`, Python; the model only where noted). Each business folder is read into
   cleaned tables:
   - header detection (title rows, 2-row headers, one sheet per month); a file that cannot be read is skipped;
   - column roles found by README description, by name and by value shape (the model proposes roles for columns
     the rules leave open, and maps unknown status words; each proposal must pass a value check);
   - Vietnamese dates and money parsed, unit conversion;
   - order header/line joins; blank cells of later rows of an order or block filled from its first row (rulebook §8).
   The tables become one DuckDB database per business, with canonical views (`sales_lines`, `orders`, debt,
   payments, imports, stock, lots...) and tested SQL macros for the train rulebook's definitions (`src/datamate/sql/`).
3. **Typed slots** (`src/datamate/slots/`, one model call per slot and shop, only when its trigger finds a gap in
   the data). For a shop convention the rules cannot read, the model fills a narrow JSON from the README, the
   rulebook rows and the column values, and code applies it, then checks it against the data (a fill that fails its
   check is dropped):
   - `order_key`: the order code column when no column has that role;
   - `status_text`: the column and words that mark a cancelled or returned order when there is no status column;
   - `name_map`: short names on the sales lines mapped to the full names of the product list;
   - `line_amount`: a line discount still inside the line amount;
   - `unit_factor`: lines sold in another unit than the base unit with no conversion factor (`1 thùng = 24 lon`).
   No slot fires on any train shop; each was checked on train shops changed to show its gap (`datamate slots --mutate`).
4. **Rulebook.** The split's `rulebook.md` is read at run time. Each tested macro is switched off when the rulebook
   no longer states its rule; the lines this rulebook adds or changes are shown to the model with each question
   that touches them, and route that question to the thinking tier.
5. **Shop audit** (`src/datamate/audit.py`, no model). Cross-checks of each shop's data that need no answer key:
   status words in a note column while the sales have no cancelled order, a line discount not taken off the amount,
   a README sentence that names a known column the roles missed, order fields given on the first row only, units
   still without a conversion factor. Each flag records what it puts at risk (a table or a column); a shop with a
   flag other than the units one is `suspect`.
6. **Per question** (`src/datamate/agent/pipeline.py`):
   - rule-based normalization: Telex typos, slang, amounts, dates (lexicon: see Data); a model rewrite only when
     words stay unexplained;
   - time hints from `calendar.csv`, including the lunar calendar;
   - retrieval of similar train questions as examples (train split only);
   - the model writes one DuckDB query (thinking off, 2048 tokens; thinking on, 16384 tokens, for questions on
     changed rules, unfamiliar wording or a failed first answer), which runs on a read-only connection with the
     question's `as_of` date bound;
   - answer checks: expected type, period, sign and plausibility. A failed check triggers one repair round; an error
     message of the database (unknown column or table) comes back with the columns and tables that exist.
7. **Choosing the answer.** The final answer is formatted as the official type. When the model finds the
   question unanswerable or hedges, a second model call (judge) re-reads the evidence and confirms, retries,
   or abstains. Deterministic gates (`src/datamate/agent/gates.py`) then abstain on answers measured unreliable on
   train (a count the size of a money amount, a table the question did not ask for...). On a `suspect` shop, a
   number, percent, string, list or table answer abstains when its query reads what a flag puts at risk (the
   tables and columns come from the database's query plan).

A question that raises an unexpected error abstains (the error and its traceback are in its log); a shop whose data
cannot be set up abstains on its questions; the run goes on with the rest. No answer or logic is keyed on question
or business IDs.

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
- **Generated data, one asset:** `assets/lexicon_external.json`, 85 word rewrites used by the question
  normalization (abbreviations, teen-code, dialect words → standard words):
  - 67 were generated with Gemini (`gemini-3-flash-preview`, temperature 0, through its API, during development only;
    inference makes no API call). The seeds were standard words of the **train** questions and train rulebook; no test
    question was sent;
  - 16 come from public web pages (the URL is stored with each entry), 2 are written by hand (`đh`, `dh` → `đơn
    hàng`, initials of a train phrase);
  - every entry passed automatic filters and a second model check. Generator code:
    `src/datamate/preprocess/lexgen.py` (prompt inside); the raw replies are kept in `assets/lexicon_gemini_raw.jsonl`
    and `assets/lexicon_gemini_verify.jsonl`; checksums in `/code/training_code/data_manifest.json`.
- **Hand-written word lists in the code.** The team wrote these lists by hand from everyday Vietnamese; no model or
  data generated them. Most entries occur in the train split (questions, rulebook, READMEs or data files). The
  entries below occur nowhere in the train split:

  | list (`src/datamate/`) | what it does | entries not in the train split |
  |---|---|---|
  | `preprocess/lexicon.py` context rules | abbreviation, teen-code and dialect rewrites | `ni` → `này` after a time or shop word; `nhut` (= `nhất`) |
  | `preprocess/entities.py` `_NORTH_SOUTH` | Northern food words matched to Southern product names | `rán`, `ngao`, `đậu phụ` |
  | `preprocess/entities.py` `_PERSON_CUE` | words that mark a person's name in a question | `em` |
  | `preprocess/rewrite.py` prompt and `_COMMON` | examples for the model rewrite, filler words | `ni`, `ơi`, `nhé`, `em` |
  | `agent/hints.py` | relative-day phrases | `hôm wa` (= `hôm qua`) |
  | `agent/frame.py` `_EMPTY` | filler words ignored when reading a question | `em`, `ơi`, `nhé` |
  | `sql/values.py` `SUBJECTIVE` | a "…nhất" question on a personal quality is unanswerable | `uy tín`, `(đáng) tin cậy`, `vui vẻ`, `tốt bụng`, `yêu thích`, `đẹp` |
  | `ingest/business.py` `_CODE_NAME` | column names that hold codes, not amounts | `số ĐT` |
  | `sql/dims.py` `_UNIT_GAP` | a unit written apart from its number | `GB` |
- No public-test or private-test data or answers were used to train, tune examples, or build any asset.
- No model was trained or fine-tuned. See `/code/training_code/README.md`.

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

- Decoding: greedy (temperature 0) for every model call, fixed seed 0, `PYTHONHASHSEED=0`.
- Model server: vLLM, tensor parallel over the 2 GPUs, bf16, context 49152 tokens (a prompt plus the 16384-token
  thinking budget).
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
/code/assets/                 Telex lexicon, generated lexicon + its raw replies, train README description -> role
                              table, train rulebook
/code/tests/                  unit tests (uv run pytest -q)
/code/models/Qwen3.5-9B/      model weights
/code/training_code/          README, train data, data_manifest.json
```
