# DataMate: Zalo AI Challenge 2026

An offline agent that answers everyday Vietnamese questions over a small business's CSV, XLSX and
SQLite files.

- Rules and data analysis: [CLAUDE.md](CLAUDE.md)
- Plan of record: [docs/PROPOSAL.md](docs/PROPOSAL.md)

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
2. **Prompt** ([src/datamate/agent/prompt.py](src/datamate/agent/prompt.py)). Built in this order:
   - system: contract, helper API and the split's `rulebook.md`
   - per business: README and schema card
   - per question: retrieved train examples and the question
3. **LLM** writes a short pandas program that ends with an `answer_*()` / `unanswerable()` call.
   It can use the helpers in [src/datamate/dm.py](src/datamate/dm.py): periods (`week_prev`,
   `this_month`, `tet`, …), `rank_top`, `find`, and answer formatting with rounding.
4. **Sandbox** ([src/datamate/agent/sandbox.py](src/datamate/agent/sandbox.py)) runs each program in
   worker processes that hold the tables in memory, with a timeout. On an error the model gets one
   repair round with the traceback.
5. **Vote** ([src/datamate/agent/vote.py](src/datamate/agent/vote.py)) takes N samples and returns
   the answer, unanswerable or abstain. Results go to `submission.jsonl`, one log per question
   goes to `logs/`, and on train the run is scored with
   [src/datamate/eval/scorer.py](src/datamate/eval/scorer.py) (an exact PA re-implementation).

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

# Claude upper bound / example-bank builder (train only, needs ANTHROPIC_API_KEY, costs money)
uv run datamate run --backend claude --model claude-opus-5-5 --limit 50 --out outputs/claude_50
uv run datamate export-examples outputs/claude_50    # verified programs -> assets/examples.jsonl

uv run datamate score outputs/<run_dir>              # re-score a run
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

### On Kaggle (2× T4) or a rented GPU with vLLM

```bash
pip install -e . vllm
vllm serve Qwen/Qwen3-14B-AWQ --tensor-parallel-size 2 --max-model-len 32768 \
    --enable-prefix-caching --dtype half &          # T4: fp16 / AWQ only
datamate run --backend local --base-url http://localhost:8000/v1 --model Qwen/Qwen3-14B-AWQ \
    --thinking off --concurrency 16 --limit 300
```

## Compliance guardrails (built in)

- The `claude` backend refuses to run on any split except `train`. Test data never goes to an
  external API.
- Examples are retrieved only from train, and questions from the same business are excluded.
- There is no logic keyed on business or question IDs.
- `/data` is read-only; caches and outputs live in `cache/` and `outputs/`.

---

## Status (2026-11-01)

**First real scores (Qwen3.5-9B Q4 via Ollama on an M2 Pro, thinking off, 1 sample, ~30 s/question)**
- `outputs/dev120_v1` (baseline): PA = 0.100 (57 correct / 45 wrong / 18 abstain).
  Main causes of wrong answers: revenue via order totals or `== 'hoàn thành'` (should be line
  amounts, not cancelled/returned, 'đang giao' counts); status normalizer bugs (`Không giao được`,
  `hàng trả về`); `1tr134` money not parsed; walk-in customers in rankings; counting rows instead of orders.
- Fixes: canonical `sales_lines()` / `revenue(p)` / `is_walkin` helpers ([src/datamate/canon.py](src/datamate/canon.py)),
  README placeholder file names (`BanHang_T<tháng>_<năm>.xlsx`), header-name role fallback, status
  from `Ghi chú`, voucher/shipping lines excluded. Simple monthly/quarterly revenue now matches gold on
  every train business that has line items.
- `outputs/dev120_v2`: after these fixes (see below once finished).
- Not yet covered by `sales_lines()`: dev_003/006/009/012, biz_110/123/124 (no line table, sqlite, or no order date).

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
- ⬜ First public-test submission (baseline)

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
