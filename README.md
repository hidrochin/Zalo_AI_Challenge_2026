# DataMate: Zalo AI Challenge 2026

An offline agent that answers everyday Vietnamese questions over a small business's CSV, XLSX and
SQLite files.

- Rules and data analysis: [CLAUDE.md](CLAUDE.md)
- Plan of record: [docs/PROPOSAL.md](docs/PROPOSAL.md); research plan (Vietnamese, for the mentor):
  [docs/PROPOSAL_V3.md](docs/PROPOSAL_V3.md), semantic-aware code generation and experiments E0–E4
- Current plan (Vietnamese): [docs/PLAN_TEXT2SQL.md](docs/PLAN_TEXT2SQL.md), fast branch and text-to-SQL on DuckDB

## Architecture

```
  data/<biz>/  CSV · XLSX · SQLite · README.md
        │
        ▼
 ┌──────────────┐   once per business, cached in cache/ingest/
 │ 1. Ingest    │──► cleaned tables + schema card + shop name index
 └──────────────┘                    │
                                     ▼
 raw question ──► ┌──────────────┐  rules: Telex residue, abbreviations / teen-code /
                  │ 2. Preprocess│  dialect / English, money + dates, shop names
                  │              │  + 1 LLM call: standard Vietnamese, answer type,
                  └──────┬───────┘  top-k, sub-questions
                         ▼
                  ┌──────────────┐ ◄── rulebook.md, business README, schema card,
                  │ 3. Prompt    │     train examples, time hints, original +
                  └──────┬───────┘     normalized question
                         ▼
                  ┌──────────────┐  Qwen3.5-9B (vLLM on Kaggle, Ollama on Mac)
                  │ 4. LLM       │  writes a short pandas program using
                  │ writes code  │  dm.py / canon.py helpers
                  └──────┬───────┘
                         ▼                  error, or the answer contradicts
                  ┌──────────────┐ ───────── the question (type, month/day, ──┐
                  │ 5. Sandbox   │           period, top-k)                   │
                  │ run + checks │ ◄──────── one repair / re-check round ─────┘
                  └──────┬───────┘
                         ▼
                  ┌──────────────┐
                  │ 6. Vote +    │──► answer │ unanswerable │ abstain
                  │ postprocess  │    in the exact output format
                  └──────┬───────┘
                         ▼
           submission.jsonl  +  logs/<id>.json
```

## How it works (current MVP)

1. **Ingest** ([src/datamate/ingest/](src/datamate/ingest/)). Each business is turned into cleaned
   pandas tables and cached in `cache/ingest/`. Ingestion:
   - finds the real header row, skipping title, filter and blank rows
   - merges monthly sheets and split files
   - parses money (`55k`, `1,6256tr`, `55.000đ`), quantities with units (`3 chai`) and mixed dates
     (dd/mm/yyyy, Excel serials, `1 thg 1 2025`)
   - assigns column roles from the README vocabulary
   - forward-fills order-level cells on continuation rows
   - drops exact duplicate rows within an order
   - adds normalized status columns (`<col>__norm`)

   It writes a **schema card** per business to `outputs/ingest/<split>/<biz>.md`.
2. **Preprocess** ([src/datamate/preprocess/](src/datamate/preprocess/)). Deterministic rules first, then one
   short LLM call:
   - `telex.py`: Telex keys left in the text (`soos` → số, `dduwowcj` → được, `cuwar` → cửa). A token is
     decoded only into a well-formed syllable (onset + a rhyme from `assets/vi_rhymes.txt`), so `check`,
     `plus` and brand words stay.
   - `entities.py`: product / customer / staff / supplier / group names of the shop found in the question,
     with diacritics respected (`tháng` is not `Thắng`). Exact matches get the data spelling and are
     protected from the other rules; partial product matches are listed for the model to check.
   - `lexicon.py`: abbreviations, teen-code, dialect and English (`dthu`, `bnhiu`, `z`, `mik`, `mô`, `ni`,
     `rứa`, `revenue`, `bill`, `hot`), the ambiguous ones only in context (`k` after a number is money).
   - `amounts.py`: `3tr7` → 3.700.000 đồng, `152k`, `18 triệu`; `th7/26`, `3/25`, `6-2025` → tháng M/YYYY,
     `quý 2/2025` → quý 2 năm 2025.
   - `rewrite.py`: one LLM call (greedy, ≤ 400 tokens) returns `{normalized, answer_type, top_k,
     sub_questions, entities}`. A rewrite that changes a number, loses an exact name or drops a word of
     the shop's names is discarded.

   Time hints, retrieval and the answer expectation all use the rule text; the solver sees the original
   question and the normalized one.
3. **Prompt** ([src/datamate/agent/prompt.py](src/datamate/agent/prompt.py)). Built in this order:
   - system: contract, helper API and the split's `rulebook.md`
   - per business: README and schema card
   - per question: retrieved train examples, the question (original + normalized, sub-questions, expected
     answer, shop names, amounts) and the time hints from [src/datamate/agent/hints.py](src/datamate/agent/hints.py)
4. **LLM** writes a short pandas program that ends with an `answer_*()` / `unanswerable()` call.
   It can use the helpers in [src/datamate/dm.py](src/datamate/dm.py): periods (`week_prev`,
   `this_month`, `tet`, …), `rank_top`, `find`, and answer formatting with rounding.
5. **Sandbox** ([src/datamate/agent/sandbox.py](src/datamate/agent/sandbox.py)) runs each program in
   worker processes that hold the tables in memory, with a timeout. On an error the model gets one
   repair round with the traceback. [src/datamate/postprocess.py](src/datamate/postprocess.py) then checks
   the answer against the question; a contradiction gets one re-check round:
   - answer type and month vs day, from cue patterns that are 100% precise on train gold (808 questions);
     if the re-check keeps it, the question abstains. The rewrite's type guess is only a soft check.
   - a date answer must lie inside the asked period (train gold: 141/141)
   - a top-k list or table has at most k items (train gold: 120/120)
   - data-free invariants (`invariants`): rates in [0, 100], integer counts, day counts within the period, a
     sign only when the question asks for a difference, money under 1000 đ, walk-in customers in rankings
   - semantic shadow check (`shadow_check` in the sandbox): when the question asks exactly one contract
     quantity ([src/datamate/eval/semantic.py](src/datamate/eval/semantic.py)) over exactly one period, the
     answer is recomputed with the `dm` primitive; a mismatch gets the re-check with the right call
6. **Vote** ([src/datamate/agent/vote.py](src/datamate/agent/vote.py)) takes N samples and returns
   the answer, unanswerable or abstain. `finalize()` writes it in the official format (integers for whole
   numbers, percent to 2 dp, `YYYY-MM[-DD]`, column-major tables transposed, anything malformed → abstain).
   Results go to `submission.jsonl`, one log per question (with the preprocessing record) goes to `logs/`,
   and on train the run is scored with [src/datamate/eval/scorer.py](src/datamate/eval/scorer.py) (an exact PA
   re-implementation).

## Quickstart

```bash
uv sync                                              # install
uv run pytest -q                                     # unit tests (parsers, scorer, helpers)
uv run datamate ingest --split train                 # cache tables + write schema cards
uv run datamate ingest --split public_test

# plumbing check, no model needed
uv run datamate run --backend dummy --limit 20 --out outputs/smoke_dummy

# local model via Ollama: Qwen3.5-9B with a 24k-context alias (prompts are up to ~16k tokens)
ollama pull qwen3.5:9b
printf 'FROM qwen3.5:9b\nPARAMETER num_ctx 24576\n' > /tmp/Modelfile && ollama create qwen3.5-9b-24k -f /tmp/Modelfile

# stratified train dev set (120 q, all answer types, all 30 businesses), low-resource Mac run
uv run datamate make-dev                             # -> assets/dev120.txt
nice -n 10 uv run datamate run --model qwen3.5-9b-24k --thinking off --ids-file assets/dev120.txt \
    --concurrency 1 --exec-workers 1 --out outputs/dev120_v1

# a single business, or specific questions
uv run datamate run --model qwen3.5-9b-24k --thinking off --biz biz_103 --limit 10
uv run datamate run --model qwen3.5-9b-24k --thinking off --ids q_001883280e83 q_00195e1e4049

# Gemini on train (needs GEMINI_API_TOKEN in .env)
uv run datamate run --backend gemini --model gemini-3-flash-preview --ids-file assets/dev60.txt --normalize rules \
    --concurrency 2 --out outputs/gemini_60

# Claude upper bound / example-bank builder (train only, needs ANTHROPIC_API_KEY, costs money)
uv run datamate run --backend claude --model claude-opus-5-5 --limit 50 --out outputs/claude_50
uv run datamate export-examples outputs/claude_50    # verified programs -> assets/examples.jsonl

uv run datamate score outputs/<run_dir>              # re-score a run

uv run datamate normalize --split public_test        # rule preprocessing of every question -> outputs/normalize/
uv run datamate validate outputs/submission_v2.jsonl # submission format and ids
uv run datamate build-lexicon                        # rebuild assets/vi_rhymes.txt from train text

uv run datamate compare outputs/<run_a> outputs/<run_b>   # paired ΔPA, bootstrap CI, transitions, outcomes
uv run datamate eval-checks outputs/<run> ...        # offline: answer checks on gold (false alarms) and on logged runs
uv run datamate semantic-check                       # contract tests of dm primitives against train gold (~18 s, no LLM)

uv run datamate sql-build --split train              # one DuckDB database per business -> cache/duckdb/ (also public_test)
uv run datamate sql-check                            # contract tests of the SQL macros against train gold (no LLM)
uv run datamate run --engine sql ...                 # the model writes one DuckDB SELECT instead of a pandas program
```

**Run directory contents:** `submission.jsonl`, `logs/<id>.json` (prompt examples, every
sample's reply, code, stdout and error, the vote, elapsed time), `report.json` (PA by type and
by business), `summary.json` and `config.json`. Runs resume by default; pass `--no-resume` to
start over.

**Useful flags:**
- `--samples 3`: greedy plus 2 seeded samples, then a vote
- `--min-share` / `--unans-share`: abstention thresholds
- `--k-examples`
- `--thinking on|off`: Qwen3.5 thinking switch (vLLM chat template; `reasoning_effort` on Ollama)
- `--ids-file assets/dev120.txt`: run a fixed question list
- `--repair 0|1`
- `--normalize off|rules|llm`: question preprocessing (default `llm` = rules + one rewrite call)

### On Kaggle (2× T4) or a rented GPU with vLLM

```bash
pip install -e . vllm
vllm serve Qwen/Qwen3-14B-AWQ --tensor-parallel-size 2 --max-model-len 32768 \
    --enable-prefix-caching --dtype half &          # T4: fp16 / AWQ only
datamate run --backend local --base-url http://localhost:8000/v1 --model Qwen/Qwen3-14B-AWQ \
    --thinking off --concurrency 16 --limit 300
```

## Compliance guardrails (built in)

- The `claude` and `gemini` backends refuse to run on any split except `train`.
- Private test data never goes to an external API. Public test data may go to Gemini only to build evaluation
  labels (the team confirmed this with the organizers on 2026-11-05); those labels never feed training, few-shot
  examples or rule vocabularies.
- Examples are retrieved only from train, and questions from the same business are excluded.
- There is no logic keyed on business or question IDs.
- `/data` is read-only; caches and outputs live in `cache/` and `outputs/`.

---

## Status (2026-11-05, evening): direction change

**Decisions after the mentor review**
- Try text-to-SQL on DuckDB, plus a no-LLM fast branch, per-business rules, entity variants and more primitives.
  Plan: [docs/PLAN_TEXT2SQL.md](docs/PLAN_TEXT2SQL.md). Started: [src/datamate/sql/](src/datamate/sql/) has entity
  dimensions and one DuckDB database per business, not yet wired into the pipeline. All 44 databases build, and the
  macros match train gold on 766/766 computed contract questions. 212 tests pass.
- 2026-11-06 (`INGEST_VERSION` 12, `SEMANTIC_VERSION` 2026-11-06.1): subtotal rows with a blank date cell are dropped (589
  rows: dev_008, biz_105); ingest warns about rows left without an order id; unknown walk-in labels are found by order
  share (`busy_customers`).
- Entity keys are in `build_sales` (since `SEMANTIC_VERSION` 2026-11-05.1): `product_name` and `customer_name` now hold one
  spelling per entity, with the source spelling in `*_name_raw`. On 60 train questions with `gemini-3-flash-preview`,
  PA rose 0.82 → 0.95 (54 / 5 / 1 → 58 / 1 / 1, ΔPA 95% CI [0.02, 0.28]). The gain may be optimistic, because these 60
  questions exposed the bug, and it is not measured on Qwen3.5-9B.
- Public labels are **on hold**: the team does not need public results yet, and self-hosting on Vast AI may be cheaper.
  Nothing from public test has been sent. The `gemini` backend refuses any split but train. If it reopens, the pick is
  `gemini-3-flash-preview` (plan §6, `outputs/gemini_pilot/`).
- Deadline: **2026-11-15**. The plan has a day-by-day schedule, and 13–15/11 stay reserved for Docker.
- pandas code generation is **paused**, not removed. The team decides later, using gate G1 in the plan.
- Docker, vLLM and Blackwell work is deferred.
- Time budget: private size is unknown. Assume 500 questions (43 s each), and design for 1000 (21.6 s each).

**Code generation: state when paused**

| item | state |
|---|---|
| Code | commit `fad0f45`, `SEMANTIC_VERSION` 2026-11-03.1, `INGEST_VERSION` 11, 157 tests pass |
| Flow | preprocess → prompt → one program → sandbox → checks → one repair round → finalize; runs end to end on Kaggle 2× T4 |
| Best public score | PA **0.288**, about 550 / 262 / 188, 4.6 s/question, on `98d4c7b` |
| Train score | PA 0.473 from E0, on code **older** than `98d4c7b` (see the correction below) |
| Current code on train | **not measured** |
| Module contribution (E1–E3) | not measured; `--checks off|structural|all` not added |
| Run-to-run variability | not measured |
| Few-shot examples | no code: `assets/examples.jsonl` does not exist, so examples carry only question and answer |
| Semantic layer | `revenue`, `avg_order_value`, `price_on`, `sales_lines`, `orders`, periods, rank and answer helpers; no debt, stock or profit primitive |
| Known defects | contracts are not gated on rulebook clauses |
| Largest failure group | E0: `KeyError` 186, `TypeError` 70, validation abstains 102 |
| Paused experiments | E1–E5 in [docs/PROPOSAL_V3.md](docs/PROPOSAL_V3.md) |

To resume: rerun train on the current code with `DM_KERNEL=<name> DM_STAGES=train kaggle/push.sh update` (about 3 h),
then continue from "Next steps" below. That rerun is also the baseline for gate G1.

**Correction to the section below.** E0 did not run `98d4c7b`. Its `config.json` has no `normalize`,
`semantic_version` or `ingest_version`, and its 3000 logs have no `pre` or `expect`, so it ran earlier code without
preprocessing, the new checks or the shadow check. Only the public run used `98d4c7b`. The two scores are not
comparable.

## Status (2026-11-05)

Public run: commit `98d4c7b`, `SEMANTIC_VERSION` 2026-11-03.1, `INGEST_VERSION` 11, Qwen3.5-9B fp16 on Kaggle 2× T4,
`--normalize llm`, 1 greedy sample, concurrency 16. E0 used older code; see the correction above.

| run | kernel | output | PA | correct / wrong / abstain | precision | time |
|---|---|---|---|---|---|---|
| E0: all train | `datamate-public-run` v8 | `outputs/kaggle/result_train/out/train` | **0.473** | 1991 / 572 / 437 | 77.7% | 2 h 46, 3.3 s/question |
| 3rd public | `datamate-public-run2` v1 | `outputs/kaggle/result_public_v7` | **0.288** | about 550 / 262 / 188 (implied by PA) | about 67.7% | 1 h 17, 4.6 s/question |

**E0 by type (PA):** boolean +0.783, percent +0.756, date +0.531, number +0.445, unanswerable +0.357,
list +0.150, string +0.045, table −0.526 (0 correct of 19). `number` holds 400 of the 572 wrong answers.

**Abstain causes:** E0: 335 execution failures, 102 validation. Public: 123 execution failures, 62 validation,
3 format. In E0 the execution failures are mostly `KeyError` 186, `TypeError` 70, `ValueError` 29,
`IndexError` 17, which points at column names and types.

**Weakest train shops:** biz_109 +0.127, biz_124 +0.246, biz_123 +0.280, biz_106 +0.286.

**Not yet known:** how much each module added. Public rose 0.040 → 0.288 with preprocessing, checks and the
new primitive all added at once, so E1–E3 still have to separate them.

**Next steps (handoff)**
1. Analyse E0 from `outputs/kaggle/result_train/out/train` (`logs/<id>.json`, `report.json`). No per-group
   analysis is done yet: wrong answers by intent, family and error kind are still open.
2. Compare the 102 validation abstains with gold, to count the correct answers the checks rejected.
3. Look into the execution failures (`KeyError` first): 11.2% of train, 12.3% of public.
4. Add `--checks off|structural|all`, then run the experiments in priority order (PROPOSAL_V3 §5): E3, E2, E4,
   E1, E5. Run each at full-train size on Kaggle. The repeat run for run-to-run variability isn't done yet.
5. Decide with the mentor whether to fine-tune, and on Python or SQL (PROPOSAL_V3 §8). The proposal still says
   E0 is running.

**Kaggle notes**
- Fetch: `uvx --with six kaggle kernels output hydrochii/<kernel> -p <dir>`. It fetches one file at a time and
  can hang without an error (a socket in `CLOSE_WAIT`). If the file count stops growing, stop it and run several
  downloads in parallel with `--file-pattern 'logs/q_[0-3]'` etc. and `--page-size 200`.
- `DM_KERNEL=<name> kaggle/push.sh update` pushes to another kernel, so a running one isn't replaced.
- `DM_ENGINE=sql` runs the text-to-SQL engine. `DM_DATASET=<name>` gives a branch its own bundle (omit `update` the first
  time, to create it). `BUILD.txt` in the outputs records the branch, commit and source hash of the run.
- `DM_STAGES=public|dev120|train` picks the stages; the default is `public`.

## Status (2026-11-03, evening)

**Committed** (`bc808d8` … `33d0413`): hints fixes, preprocess, postprocess, `avg_order_value` +
`SEMANTIC_VERSION`, eval tools (compare / eval-checks / semantic-check), pipeline wiring, Kaggle train stage.
157 tests pass.

**E1 (normalize off vs llm on dev120) paused:** stopped at 75/120 of the `off` arm (`outputs/e1_off`,
snapshot before the E2/E3 changes) to free the Mac. It is superseded by running E1 on Kaggle at full-train size.

**Semantic layer (E2) and result validation (E3), measured offline:**
- `datamate semantic-check` at `SEMANTIC_VERSION` 2026-11-03.1, computed cases all exact:
  avg_order_value 167/167, revenue 259/259, order_count 136/136, cancel_count 100/100,
  cancel_rate 104/104. Not covered: 25 questions on shops without `sales_lines`, 33 with several periods.
- Invariants: 0/2930 false alarms on gold, 4/113 wrong answers of logged runs caught.
- Shadow check: 16/16 in-scope wrong answers flagged, 0/44 false alarms on correct ones.
- Risk: the shadow check assumes the private rulebook keeps these definitions; gate each contract on its
  rulebook clause before the private submission.
- To do for clean E2/E3 arms: a `--checks off|structural|all` flag (both layers are always on now).

## Status (2026-11-03)

**Leaderboard**

| submission | file | code | PA |
|---|---|---|---|
| 1st (2026-11-02) | `outputs/submission.jsonl` | Kaggle kernel v5 | −0.110 |
| 2nd (2026-11-03) | `outputs/submission_v2.jsonl` | Kaggle kernel v6, commit `17129c4`, `INGEST_VERSION` 11 | 0.040 |
| 3rd (2026-11-05) | `outputs/kaggle/result_public_v7/submission.jsonl` | kernel `datamate-public-run2` v1, pipeline `98d4c7b` | **0.288** |

**Second public submission: PA 0.040**
- The code includes every 2026-11-03 fix listed below: server-error retry and stop, vLLM restart and
  resume, worker memory cap, repair for "helper unavailable" refusals, and the preprocessing fixes. The
  bundle's `INGEST_VERSION` matches local (11).
- The vLLM-on-T4 path now finishes end to end. All 14 businesses were attempted; in the first run, 340
  questions were lost after the server stopped.
- The 1000 lines are 778 typed answers, 12 unanswerable and 210 abstain. That implies about **415 correct and
  375 wrong out of 790 attempted, so precision is about 52.5%**, barely above break-even. dev120_v4 had 73%.
  The gap between train dev and public precision is now the main problem ([DATA.md](DATA.md) §4.1 point 6, §7.2).
- Compared with the first submission:
  - zero answers: 29 → 0
  - "sales_lines() not available" refusals: 43 → 0
  - unanswerable answers: 49 → 12
- Abstain share by business:
  - highest: dev_006 41/111, dev_014 26/63, dev_011 13/40, dev_003 26/129
  - lowest: dev_007 3/60, dev_002 3/41
- Only the submission file was saved, not the per-question logs, so precision by business and by type is
  unknown.
- **Unanswerables that look wrong** (spot check of the 12; each one is −1 if gold is a value):
  - `quy 2/2025`: the time hint returns `month(2025, 2)` instead of quarter 2, because the month pattern wins
    over `_QUARTER` in `hints.py`. The model then declared it out of range.
  - "Từ th 1/25 tới th 3/25, dthu tháng sau đều cao hơn tháng trước": the hint adds
    `prev_period(month(2025, 1))` = Dec 2024, which is out of range. The question only needs Jan–Mar.
  - "Đầu năm tới chừ" (dialect, "chừ" = now): no hint is produced, and the model used 2027.
  - dev_012 "Doanh thu tháng 4/2026 … %": the model said a repair shop records no revenue, but repair tickets
    carry money.

**Preprocessing and postprocessing modules (2026-11-03, committed in the evening, not yet scored)**
- New: [src/datamate/preprocess/](src/datamate/preprocess/) and [src/datamate/postprocess.py](src/datamate/postprocess.py);
  see "How it works" steps 2, 5 and 6. Default `--normalize llm`. 156 unit tests pass.
- Rules alone change 2505 of the 4000 train + public questions in 0.15 s (`datamate normalize`):
  - Telex residue: about 100 questions
  - `dthu` 646, `bnhiu` 396, `bn` 222, `z` 155, `dc`/`đc` 274
  - compressed dates: 1284
- Time hints on the rule text vs the raw question: 3898 of 4000 identical, 58 gain a period, and the rest
  are fixes:
  - `quý 2/2026` was `month(2026, 2)` on every such train question
  - `thangs 6 năm 2025` was `year(2025)`
  - `N ngày ngay trc` now gets `days_before`
- `hints.py` fixes: quarter before month; no `prev_period` for "tháng sau … tháng trước"; `đầu năm tới giờ` =
  `ytd()`; `tới giờ` = until now.
- LLM rewrite (Qwen3.5-9B on Ollama, 3 samples of train questions, 44 in total):
  - answer type right 43/44; the miss has an unanswerable gold answer
  - ~2–3 s per question on the Mac
  - 0 of 16 rewrites discarded in the last sample, after the checks were tuned
- Replayed on `submission_v2.jsonl`:
  - the hard checks flag 7 answers that are certainly wrong: 4 lists for "tháng nào", and 3 days outside the
    asked month
  - `finalize` repairs or abstains 5 malformed lines (2 transposed tables)
- 8-question end-to-end run with the local model works (PA +0.125, 4/3/1). That is too few to compare
  with v4.

**Next steps (handoff)**
1. A/B on dev120: `--normalize off` vs `llm` (about 35 min each on the Mac). Then a Kaggle public run, saving
   the per-question logs (`uvx --with six kaggle kernels output hydrochii/datamate-public-run -p
   outputs/kaggle/result_v7`).
2. Raise precision before coverage. At ~52% precision, abstention is the biggest lever (Phase 5).
3. Still open from 2026-11-01: phone-shop abbreviations, repair-shop normalization, order-only shops, debt
   helper.

## Status (2026-11-01)

**First real scores (Qwen3.5-9B Q4 via Ollama on an M2 Pro, thinking off, 1 sample, ~30 s/question)**
- `outputs/dev120_v1` (baseline): PA = 0.100 (57 correct / 45 wrong / 18 abstain).
- `outputs/dev120_v2` (sales_lines + ingest fixes): PA = 0.208 (56 / 31 / 33), 55 min. By type: number +0.36,
  percent −0.09, list −0.13, table −0.50. Next: triage percent/list/table errors.
  Main causes of wrong answers: revenue via order totals or `== 'hoàn thành'` (should be line
  amounts, not cancelled/returned, 'đang giao' counts); status normalizer bugs (`Không giao được`,
  `hàng trả về`); `1tr134` money not parsed; walk-in customers in rankings; counting rows instead of orders.
- Fixes: canonical `sales_lines()` / `revenue(p)` / `is_walkin` helpers ([src/datamate/canon.py](src/datamate/canon.py)),
  README placeholder file names (`BanHang_T<tháng>_<năm>.xlsx`), header-name role fallback, status
  from `Ghi chú`, voucher/shipping lines excluded. Simple monthly/quarterly revenue now matches gold on
  every train business that has line items.
- `outputs/wrong_v3`: the 42 questions wrong in v1/v2, rerun with shorter output (max_tokens 1024):
  −31 → −18 points, mostly wrong → abstain. None of the 38 still-failing answers was a format problem;
  all were semantic (period, missing column, walk-in, order-level logic, pandas slips, empty-filter refusals).

**Preprocessing + output checks (v4, 2026-11-01)**
- **Question preprocessing:** [src/datamate/agent/hints.py](src/datamate/agent/hints.py) resolves time expressions
  deterministically (hqua/bữa qua, tuần rồi, th7/26, tháng M without a year, "tháng trước" relative to the
  asked month via `prev_period`, cùng kỳ năm trước, N ngày kể từ ngày D / ngay trước, đến hết ngày D,
  Tết) and lists the period calls under the question. It matches gold on all 164 train "% change vs the
  previous period" revenue questions.
- **Data preprocessing:**
  - `sales_lines()` now carries the order header's other columns (channel, payment, totals, timestamp)
    and product-master attributes (group, active ingredient, prescription flag, cost); catalogue noise
    columns are dropped.
  - `kl` is a walk-in label (12 train and most public businesses); `item` was renamed `is_goods`;
    phone numbers are stripped from customer names.
  - CamelCase/snake_case headers get roles, so biz_123 and dev_006 (POS sqlite) now have `sales_lines`.
- **New helpers:** `orders()` (one row per order, `goods_amount`, `counted`), `price_on`, `prev_period`,
  `same_period_last_year`, `days_from`, `days_before`. Gold checks: cancel count 88/88, cancel rate
  114/114, order-threshold counts 22/22, `price_on` 25/25.
- **Output checks** (`check_result` in the sandbox):
  - string/list/table names must exist in the data, and True/False flags are rejected;
  - a refusal from the model after an empty filter goes to repair, and then abstains;
  - 0 or implausible percents get one re-check round.
  - All gold train answers pass the hard checks.
- Still without `sales_lines()`: dev_003/009/012, biz_110/124 (no line table or no order date).

**In flight / next steps (handoff)**
- `outputs/dev120_v4`: **PA 0.375** (72 correct / 27 wrong / 21 abstain, precision 73%), 32 min at about
  16 s/question. v2 was 0.208. By type: number +0.52, percent +0.48, string +0.50, list −0.38,
  table −0.75. Transitions from v2: 14 wrong → right, 13 abstain → right, 7 right → wrong.
- **Fixed after v4:** a 0 (or implausible percent) still flagged after its re-check now abstains. All 4
  zero answers in v4 were wrong, so this is worth about +0.03. Kaggle v5 was pushed before this fix.
- **Kaggle public run** ([kaggle/](kaggle/)): `kaggle/push.sh update` uploads the current code and data
  as the private dataset `hydrochii/datamate-bundle` and runs the kernel `hydrochii/datamate-public-run`
  on 2× T4 (P100 cannot run vLLM). By default it answers **only the public test** (`DM_STAGES=public`).
  - Model: `Qwen/Qwen3.5-9B` fp16, with automatic fallback to `Qwen/Qwen3-14B-AWQ`.
  - Fetch the result with `uvx --with six kaggle kernels output hydrochii/datamate-public-run -p outputs/kaggle/result`;
    the file to upload to the leaderboard is `submission.jsonl` in that folder.
  - Earlier attempts failed in setup (dataset auto-unzipped; vLLM installed into the wrong venv).
    Both are fixed, but no run has finished yet, so the vLLM-on-T4 path is still unverified.
- **First public submission (2026-11-02): PA −0.110.** The vLLM server stopped answering partway through dev_008, and
  the pipeline wrote abstain for every later question (dev_009–014, 340 questions). 43 answers were also
  "unanswerable" with the reason "sales_lines() not available". Diagnosis: [DATA.md](DATA.md) §4.1.
- **Fixes (2026-11-03), Kaggle kernel v6 pushed:**
  - The pipeline retries model-server errors, then stops without writing (`LLMUnavailable`; resume picks the
    questions up again).
  - `run_kaggle.py` restarts vLLM with the same model and resumes until every question is answered. It logs
    `sales_lines` per business and the host's memory and OOM state.
  - Sandbox workers get an address-space cap (`DM_WORKER_MEM_GB`, default +4 GB).
  - An unanswerable whose reason is about a helper being unavailable goes to repair.
- **Preprocessing review and fixes (2026-11-03, `INGEST_VERSION` 11).** The audit scripts are in `debug/`
  (gitignored); `debug/regress.py` holds the train-gold regression checks.
  - Fixes:
    - status `Đang vận chuyển` → huỷ bug
    - subtotal and note rows dropped
    - header roles for unseen layouts and README order keys, so biz_124 and dev_003 now have `sales_lines`
    - base-unit conversion
  - Gold checks: monthly revenue 166 → 168, cancel count/rate 202 → 204 of 208, top-k 25 → 32 of 33,
    and nothing broke.
  - Still open:
    - phone-shop abbreviations: the `Ghi tắt` sheet is joined as an attribute, but abbreviated names are not
      mapped to full names
    - repair-shop normalization (mechanic nicknames, plate and model in one cell)
    - order-only shops (biz_110, dev_009)
    - the "topping in the same cell" rule
- **Then:** debt helper (`debt_at`, aging, per-customer flow); the 19 questions wrong in every run
  (`outputs/wrong_v3`); PROPOSAL_V2 Path A (typed plan executor) on top of the new helpers.

## Status (end of 2026-10-30)

**Working**
- Ingestion of all 44 businesses (train + public), 0 warnings; schema cards in `outputs/ingest/`
- 37 unit tests passing
- Dummy-backend end-to-end run (`outputs/smoke_dummy`): every stage works

**Not yet validated: no real LLM answer so far**
- The first local test (`outputs/smoke_qwen36`, the existing `qwen3.6:35b-mlx` via Ollama with a
  32k context) hit **Metal out-of-memory** on all 10 questions. The pipeline handled it correctly
  (errors were logged and every question abstained, PA = 0).
- The 35B model is over the 15B budget anyway; it was only a plumbing test.
- A temporary Ollama alias `qwen36-32k` was created for this test. Remove it with
  `ollama rm qwen36-32k`.

**Next session, in order**
1. Get a first real score on train:
   - **Option A (Mac):** `ollama pull qwen3:8b` (about 5 GB), then create a 16k-context alias:
     ```
     printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > Modelfile
     ollama create qwen3-8b-16k -f Modelfile
     ```
     Then run:
     ```
     uv run datamate run --model qwen3-8b-16k --limit 20 --concurrency 1 --exec-workers 1 --k-examples 2
     ```
   - **Option B (Kaggle 2× T4):** use the vLLM recipe below with `Qwen/Qwen3-14B-AWQ` on
     `--limit 100`.
   - **Option C (Claude upper bound, train only, costs money):** run
     `uv run datamate run --backend claude --limit 50`, which separates ingestion and prompt
     problems from local-model weakness.
2. Read `logs/*.json` for the failures and sort them into ingestion, rule semantics, question
   understanding and format. Fix the largest bucket first.
3. Continue with the Phase 1–2 tasks below: canonical tables, rule-level helpers, date and entity
   hints.

## Tasks

Status: ✅ done · 🟡 partial · ⬜ todo. Day numbers follow [docs/PROPOSAL.md](docs/PROPOSAL.md) §7.

### Phase 0: foundation (D1)
- ✅ uv project, git, `.gitignore`, CLAUDE.md, proposal
- ✅ Local scorer (string normalization, per-position aliases, table rows as a set, PA breakdowns)
- ✅ Raw readers (calamine xlsx, csv encoding and delimiter sniffing, read-only sqlite)
- ✅ Header detection, title/filter/total row removal, union of monthly sheets and split files
- ✅ Unit tests for parsers, scorer and helpers

### Phase 1: ingestion quality (D2–D3)
- ✅ Value parsers (money suffixes, separators, units in cells, Excel serials, `thg`, dates without a year)
- ✅ README description → role mapping (order id, date, status, product, qty, unit, amount, customer)
- ✅ Forward-fill of order-level columns; dedupe of identical rows within an order; status normalization
- ✅ Schema card per business; ingest report for all 44 businesses (0 warnings)
- ⬜ Ingestion assertions per business (no NaT order dates, every line maps to a product, revenue for a sample month computed two ways)
- ⬜ Canonical tables (`orders`, `lines`, `products`, `payments`, `imports`, `stock`, `lots`) built from roles, with `qty_base` from the conversion tables
- ⬜ LLM fallback for column roles when the README description is unknown (43% of public phrases are new)
- ⬜ Cross-check `qty × price − discount ≈ amount` to catch separator and parse errors
- ⬜ Handle "status only in Ghi chú" tables (public rulebook) in `__norm`

### Phase 2: helpers and prompt (D4)
- ✅ `dm` helpers: periods (incl. Tết and pre-Tết from the lunar calendar), `in_period` clipped to AS_OF, coverage guard, shifts, weekday counts, `rank_top`, `find`, answer formatting with half-up rounding
- ✅ Prompt: system (contract + API doc + rulebook), business (README + card), question (examples + as_of with weekday)
- ⬜ Rule-level helpers on canonical tables: `valid_orders`, `revenue`, `qty_sold`, `cancel_rate`, `debt_at`, `debt_aging`, `stock_at`, `gross_profit`, `slow_movers`, `nearest_expiry_lot`
- ⬜ Deterministic hints: normalized explicit dates (`th7/26`, `quý 2/2026`, `6-2025`), entity candidates from fuzzy lookup, answer-type guess
- ⬜ Measure prompt tokens per business; trim the card if needed (never trim the rulebook)

### Phase 3: example bank with Claude (D4–D5, train only)
- ✅ Claude backend (prompt caching, server-side fallback) and `export-examples` for verified programs
- ⬜ Run Claude on all 3000 train questions → `assets/examples.jsonl`
- ⬜ Triage Claude failures per business → fix ingestion and helpers (main ingestion health metric)
- ⬜ Prefer examples whose capabilities match the current business

### Phase 4: local model pipeline (D5–D7)
- ✅ OpenAI-compatible local backend (Ollama / vLLM), sandbox pool, repair round, voting, resumable runs
- 🟡 Model: Qwen3.5-9B (older Qwen3 / Qwen2.5 options dropped); thinking on/off and quantization still to compare
- ✅ Stratified dev subset `assets/dev120.txt` (`datamate make-dev`: all answer types, all 30 businesses)
- ⬜ Adaptive sampling (stop early when the first samples agree)
- ✅ First public-test submissions: PA −0.110 (v5), then 0.040 (v6)

### Phase 5: abstention and calibration (D8)
- ⬜ Confidence features (vote share, errors, type-hint match, sanity checks, intent-family precision)
- ⬜ Tune thresholds on train under leave-business-out to maximize PA
- ⬜ Unanswerable policy: deterministic guards plus a strong vote, otherwise abstain
- ⬜ Second public submission

### Phase 6: generalization (D9)
- ⬜ Robustness pass on public formats (phone shop, repair shop, mother-and-baby): crashes, empty tables, odd cards
- ⬜ Leave-family-out stress test

### Phase 7: Docker and delivery (D10–D12)
- ⬜ Dockerfile (CUDA ≥ 12.8 base per the organizers' guideline, vLLM, weights baked in), `predict.sh`: `/data` → `/result/submission.jsonl` + `/result/logs/`
- ⬜ Ingestion overlapped with model loading; incremental writes; 5h30 time guard
- ⬜ Determinism check (two runs, PA difference ≪ 0.03); run with `--network none`
- ⬜ Timing dry run on rented 2× 5090; solution README listing the models
- ⬜ Freeze and submit
