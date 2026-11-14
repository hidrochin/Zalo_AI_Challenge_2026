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
- Nothing learned from the public test lives in inference code (organizers' QnA, `description/QnA.txt`): no public
  rulebook wording (only the train rulebook is stored, in `rules.TESTED`), no public-only macro, header, status word or
  layout, no fix chosen on public labels. A feature stays only when train data or the train rulebook needs it.
  Exception chosen by the user: question preprocessing (`preprocess/` lexicon and slang).

---

## Status (2026-11-15, evening): start here: typed slots 0–3 in code, Kaggle run with Qwen + thinking

Code (uncommitted, on `text2sql`), measurements and the user's decisions are in
[docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md) section 9.6 (Vietnamese). In short:

- `src/datamate/slots/`: `apply.py` (pure applies) and `base.py` (trigger, prompt, parse, check per slot;
  `resolve_slots` caches `cache/ingest/<split>/<biz>.slots.json`). Slots: `order_key` (added by the user's choice),
  `status_text`, `name_map`, `line_amount`; `non_goods` deferred (no real case in train or public).
- Wired through `canon.build_sales(..., slots)`, `sql/store.build_store(..., slots)` (`STORE_VERSION` 15), the pipeline
  (`--slots all|none|names`, default all; `slots.json` per run, `slots` in each log) and `datamate slots`.
- `_JUNK` applies to catalogues only. `sql-check` is unchanged against a control run.
- A cut repair round now gets one more try with thinking off (8 such abstains in the train route run).
- Train is a strict no-op: no slot fires on any of the 30 train shops. On public, slots fire on exactly the four
  suspect shops.
- Replay of public_d's SQL on the new stores (latent, before gates):
  - dev_013: 7/48 → 34/19;
  - dev_003: 15/85 → 65/9, and the shop is now clean;
  - dev_011: 14/25 → 35/1, still gated by `fill_gap`, kept by the user's choice;
  - dev_014: 10/38 → 27/14, still gated.
- Train mutates with gold (`datamate slots --split train --mutate ...`; baseline 826/859 contracts): `rename_order_key`
  826, `status_in_notes` 802 (97.1%), `abbrev_names` 826 with every product name back, `amount_before_discount` 826
  (718 without the slot). All four pass the 95% bar.
- Kaggle (pushed this session, Qwen3.5-9B, `DM_THINK=route`, public_d flags):
  - `hydrochii/datamate-slots-public` (stages `smoke,public`);
  - `hydrochiii/datamate-slots-train-check` (stage `train_check`, 106 ids: right → wrong must be 0);
  - `hydrochii/datamate-slots-public2` (stages `smoke,public`): the committed code, with the status fixes found by the
    mutates. The first public kernel ran the code from before them.
  - Fetch results with `kaggle/fetch_output.py`. Compare per shop with public_d, using `ground_truth.jsonl` only to
    measure.

## Status (2026-11-15): decision made, plan for typed slots (docs/PLAN_LAYERS.md section 9)

No code change in this session. The user chose **option 2 of 8.6**: the model fills typed slots, code applies them
deterministically. The plan, with slot list, code changes and acceptance order, is
[docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md) section 9 (Vietnamese). Start the next session there.

What was checked before writing it (public stores `cache/duckdb/public_test/*_i21_s14*.duckdb`):

- `sales_all` **already carries every unroled column** of the line and header tables, with the shop's own names
  (`canon._extra_cols`): dev_003 `Trạng thái giao`, `Tiền giảm`, `Phí ship`; dev_011/014 `Ghi chú`, `Ghi tắt`, `Bớt`,
  `Thu máy cũ`; dev_013 `Ghi chú`, `Kênh`, `Nhóm`, `Hãng`, `Số / Size`. The data is there; the **meaning** is not:
  `status`, `counted`, `amount`, `is_goods` are computed by role regexes that ignore those columns, and the SQL model
  queries macros over `counted`.
- Real data gaps: `canon._JUNK` drops `thuộc tính`, `bảo hành`, `mô tả`, `STT` by name; side tables stay `raw_*`
  without a fact (dev_014 debt, installment, warranty, trade-in; dev_013 promotions); dev_012 has no `sales_all` at all
  (repair log, no column matches `line_amount`).
- The model already intervenes at two narrow points (`agent/statuses.py`, `agent/columns.py`), but both run only when
  the rules know they are missing something. dev_013's status in `Ghi chú` is never asked about because the status
  column "exists" (default `hoàn thành`). Slot triggers therefore fire on *gaps in the data*, not on rule failures.
- Abstains of public_d re-checked per gated answer against gold: gate `shop_suspect` blocked 212 answers, 181 of them
  wrong (dev_003 6/87 right, dev_011 12/32, dev_013 5/50, dev_014 8/43). The gate is right; the store is the cause.

Seven slots in order: status in a text column (from the probe), abbreviation name map, line discount, non-goods lines,
attribute roles, side tables, sales from a log table. Each has a trigger, a narrow JSON schema, a pure apply, a
physical check, a train no-op condition and a gold check by a train mutate (`eval/mutate.py`). Fallback is always the
current store plus the gate, so no step can score below public_d.

## Status (2026-11-14, night 2): public_d 0.595, train_check 0.953, store repair probe (P3)

Code in the repo is unchanged since `291d1bf`; this session only measured. Measurements in Vietnamese:
[docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md) section 8.6. Docker image #1, the server and the run commands are in the
section below ("late night").

| run | code | PA | right / wrong / abstain |
|---|---|---|---|
| public_d (`outputs/rented/public_d`; public by `ground_truth.jsonl`, measure only) | `291d1bf` | **0.595** (public_c 0.577) | 649 / 54 / 297 |
| train_check (`outputs/rented/train_check`, 106 train ids) | `291d1bf` | **0.9528** (0.9151 before the gates) | 101 / 0 / 5 |

- train_check vs the route-train run on the same 106 ids (88 / 18 / 0): W→R 13, W→A 5 (`table_words` 4), **R→W 0**.
  The late-night lints hold on train, so `291d1bf` is fit for image #2 (in-sample: the lints come from these errors).
- `datamate gate` needs a run-root `audit.json`: merge `part*/audit.json` first (done for public_c, public_d,
  train_check).
- public_d per shop: known families +0.76..+1.00 (dev_009 1.000); dev_003 +0.054, dev_011 −0.025, dev_012 −0.020,
  dev_013 +0.018, dev_014 +0.016.
- public_d abstains by layer (297):
  - gate `shop_suspect` 212, of which 181 would have been wrong and 31 right;
  - SQL error 56;
  - reply cut by `length` 15: with `--repair 1` the no-thinking retry in `_samples` has no attempt left;
  - unconfirmed 7, judge 3, other gates 3, finalize 1.

**Why new industries fail** (replay of public_d's logged SQL on rebuilt stores, `debug/p3_slot/replay.py`; the
control reproduces 230/230 answers). Before the gate, the four suspect shops answer 40 right / 189 wrong. The model's
SQL is mostly right; the store misses the shop's own conventions. For example, dev_013's August 2025 revenue equals
gold exactly once the orders marked `cancel`/`Huỷ` in `Ghi chú` are dropped. A better question-side model would query
the same wrong store.

**P3 probe with Qwen3.5-9B** (scratch code in `debug/p3_slot/`, gitignored; nothing in `src/`):
- The model rewrites the whole `sales_all` as one SELECT (`p3_probe.py`): **fails**.
  - Thinking is cut at 12k and at 20k tokens.
  - Without thinking the SQL runs but is wrong: inverted `is_goods`, `Huỷ` missed, tables that do not exist.
  - dev_003 and dev_014 never produced a SELECT that runs.
- The model fills a typed slot and code applies it (`slot_probe.py` + `patches/generic_slot.py`): **works**.
  - Slot: `{status_column, status_map → the rulebook's 4 statuses, order_column}`.
  - Prompt input: the README, the rulebook rows on status, and the values of the unroled columns.
  - About 100 s per shop; code applies the worst status per order.

| shop | before (right / wrong) | after | W→R | R→W | slot |
|---|---|---|---|---|---|
| dev_013 | 5 / 48 | 34 / 19 | 30 | 1 | `Ghi chú`: cancel, Huỷ → huỷ |
| dev_003 | 13 / 81 | 25 / 69 | 13 | 1 | `Trạng thái giao` (4 values), `Đơn số` as the order key |
| dev_011 | 14 / 22 | 20 / 16 | 6 | 0 | return notes in `Ghi chú` (the audit's 0.5% threshold missed them) |
| dev_014 | 8 / 38 | 8 / 38 | 0 | 0 | no status problem |

- The model's `order_column` is unreliable: it proposed `STT`, a row number. Use it only when the store has no
  `order_id`.
- After the repair, dev_013's audit flag clears, so its gate opens: 34/53 latent right, about +15 public answers.
  dev_003 (`discount_unapplied`) and dev_011 (`fill_gap`) stay gated, correctly: 27% and 56% right.
- Still wrong after the status slot: 142 of 229. Each remaining cause is another convention:
  - abbreviation sheet `Ghi tắt`: `name_gap` 0.80–0.90 on dev_011/014;
  - line discount (dev_003);
  - product group and size attributes (dev_013);
  - ship, voucher and points lines (sign, zero).

**Next.**
1. User decision, compliance of the status slot:
   - in the train rulebook: rows 109/110 (four statuses; an order's status applies to all its lines);
   - public rulebook only: row 143 ("no status column → status in Ghi chú"); the model reads it at run time, the
     code does not encode it;
   - the slot idea came from analysing public.
   - If integrated, drop "ghi chú" from `slot_probe.rule_rows`'s regex (row 143 matches "trạng thái" anyway).
2. If yes, make it a prepare step:
   - run only when every canonical status is `hoàn thành`;
   - validate on train: shops without a status column must get no harmful mapping; `sql-check` and a replay of the
     route-train logs must not change a right answer;
   - bump `STORE_VERSION`, then a live public run.
3. Same slot pattern for the next conventions, in order of public loss: `Ghi tắt` names, line discount, product
   attributes, non-goods lines.
4. Image #2 from `291d1bf` (or later) with `docker/build_remote.sh BUILD=1` once #1 is uploaded.
5. Gemma 4 12B is not tested. Test it on the slot task only, which needs stopping Qwen; server vLLM is 0.32.0 and
   `run_kaggle` notes 0.33.0 for Gemma 4.
6. Optional: the `length`-cut retry with `--repair 1` (15 public abstains); measure on train logs first.

Server at handoff: Qwen still served on `ssh -p 10102 root@45.143.122.52`, GPUs idle, no run in progress. Tunnel for
the probes:
`ssh -f -N -o ServerAliveInterval=15 -L 18000:127.0.0.1:18000 -p 10102 root@45.143.122.52`. It dropped twice during
the session, so check `curl 127.0.0.1:18000/v1/models` before a probe.

## Status (2026-11-14, late night): first Docker image built, public_c 0.577, train-backed lints

Plan and measurements: [docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md) sections 6.5, 8 and 8.5 (Vietnamese). Pipeline
walkthrough: [docs/PIPELINE.md](docs/PIPELINE.md).

| run | code | PA (public by `ground_truth.jsonl`, measure only) | right / wrong / abstain |
|---|---|---|---|
| public B (`outputs/rented/public_b`) | before section 7 | 0.406 | 664 / 258 / 78 |
| public_c (`outputs/rented/public_c`) | P1, P1b, P2, P4, P5, `order_goods` README role | **0.577** (0.590 replayed without the `length` gate) | 633 / 56 / 311 |
| public_d (`outputs/rented/public_d`, server) | public_c + the fixes below | running at handoff | |
| train_check (`outputs/rented/train_check`, server) | same | running at handoff: 106 train ids (`outputs/rented/train_check_ids.txt`) = 46 hit by the new advice or answered as a table + 60 random controls | |

**Committed in this session** (branch `text2sql`, not pushed):
- Shop audit (`src/datamate/audit.py`, flags `role_gap`, `status_in_text`, `discount_unapplied`, `fill_gap`; feature
  `name_gap`), `audit.json` per run, gate `shop_suspect` (a suspect shop keeps only `date`, `boolean`, unanswerable).
- Per-question gates (`src/datamate/agent/gates.py`, `datamate gate <run> --gold ...`): `count_money`, `items_len`,
  `table_words`, `unans_in_sql`, `expect_type`. The `length` gate was removed (−14 train, −13 public_c).
- Ingest 23: rulebook §8 fill of first-row-only order columns and of dated blocks (`_group_fill`, `_block_fill`);
  README role `order_goods` from the train description dictionary (`assets/readme_roles.json`).
- `parse_macros` drops repeated macro names; compile off by default (`--catalog delta` keeps the text delta only).
- New tonight, each from a train error of the route run (first attempts in the train logs: 14 wrong hit, 10 right hit
  where the advice is still correct, e.g. `count(*)` on lines that happened to rank right):
  - `lint.frame_advice`, one rewrite and never a block: `shift_orders` for "số đơn" per shift (biz_105/106), missing
    `pct_change` (biz_117), `count(*)` on `sales_lines` for orders (biz_121), guessed product code on a raw stock report
    (biz_120 ×4), `stock_split` unit ≠ asked unit (biz_121), frame customer group dropped (biz_123), SQL days disjoint
    from the frame period (biz_128).
  - `sql.agent.drop_code_column`: a code column before the name column leaves a table answer (biz_127; 19/19 train
    gold tables are name, amount).
  - Lexicon: `đh`/`dh` → `đơn hàng` (preprocessing exemption; 0 train questions contain it; 8 public_c wrongs).
- Rejected after measuring: guessed staff-code lint (1 train case; re-asks 3 right public answers), "frame macro not
  called" lint (train 3 right / 3 wrong).
- `docker/assemble.py` (daemonless `docker save` archive), `docker/build_remote.sh` (stage /code + venv on the server,
  `BUILD=1` assembles), `remote/` scripts. 391 tests pass.
- Not committed on purpose: `assets/oracle/` (debug notes keyed by question id, now in `.gitignore`).

**Docker image #1** (built 2026-11-14 14:23Z, code = working tree of public_c minus the `length` gate, i.e. before
tonight's lints): `zaic2026_datamate_oom:final`, 43 layers, CMD `bash /code/predict.sh`, WORKDIR `/code`.
`.tar.gz` 26 739 878 669 bytes, MD5 `2992a15d5d4f8c29d75a7f885fa97de5`, uncompressed `.tar` 47 241 054 208 bytes.
Downloading to `~/Downloads/zaic2026/` at handoff (about 4 MB/s); then `md5 -r` must match, and the user uploads the
`.tar.gz` through the browser to a friend's Drive folder (counts against the uploader's quota unless it is a Shared
Drive), sets "Anyone with the link", and sends link + MD5 + `.tar` size. The image has never run (no docker on the
server, no GPU on the Mac); the organizers do not test-run images.

**Server** (vast.ai 2×4090, `ssh -p 10102 root@45.143.122.52`; key passphrase: ask the user, load with ssh-add, never
store it). vLLM serves `llm` on 127.0.0.1:18000 (128 seqs, 16384 batched tokens, util 0.92). Code for runs in
`/workspace/zalo_ai` (rsync `src assets tests pyproject.toml uv.lock remote`; `data/train/data` now copied too);
image build in `/workspace/imgbuild` (base blobs, `ctx/models`, `ctx/training_code`), image code in `/code` (+ `.venv`).
Disk: about 23 GB free while the 26.7 GB archive sits in `out/`.
```bash
# read the runs (on the Mac)
rsync -az -e "ssh -p 10102" root@45.143.122.52:/workspace/zalo_ai/outputs/rented/{public_d,train_check} outputs/rented/
uv run datamate gate outputs/rented/public_d --gold ground_truth.jsonl          # audits merged from part*/audit.json
uv run datamate gate outputs/rented/train_check --gold data/train/answers.jsonl
# image #2 after the upload of #1 (frees the disk): commit, then
SSH="ssh -p 10102 root@45.143.122.52" BUILD=1 docker/build_remote.sh
```

**Next.**
1. Read public_d and train_check: train controls must not drop; the 46 advice/table ids should gain; public by labels
   only to report. Keep a lint only if train says so.
2. If they hold: image #2 with `docker/build_remote.sh BUILD=1` (only the code layer changes; about 25 min + download).
3. User decisions (PLAN_LAYERS 8.4): customer-group headers `Loại khách`/`Kiểu khách` read via the README sentence
   "nhóm khách" (5 public wrongs; no train case of a missed header), `unfamiliar_industry` level for dev_012-like shops,
   debt written in notes.
4. P3 (LLM store patch for suspect shops) only after an image is submitted.

## Status (2026-11-14, evening): public 0.406 on a rented 2×4090, compile and router fixes, shop gate

Measured results per layer are in [docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md) section 6 (Vietnamese). Short version:

| run | where | time | PA | right / wrong / abstain |
|---|---|---|---|---|
| train route (`hydrochiii/datamate-route-train`, B flags) | Kaggle 2×T4 | 7.6 h | 0.9707 (in-sample) | 2941 / 29 / 30 |
| public B (`outputs/rented/public_b`) | vast.ai 2×4090 | 28 + 24 min | **0.406** | 664 / 258 / 78 |

- **Why public fell from 0.751 (v5) to 0.406:** ingest, not the model. Commit `7ca2324` (compliance) removed the
  public-only ingest, and the six worst shops (dev_003, 009, 011–014) collapse. The Mac store gives the same wrong
  numbers as the server.
- **A learned L8 is useless on the train run** (LOBO drops 0 answers). Four deterministic gates measured on train are
  not coded yet (PLAN_LAYERS 6.2).

**Uncommitted changes (369 tests pass):**
- `pipeline._think`: a compile/shop-view reply cut empty by thinking is asked again with thinking off.
  `sql/compile.py`: `COMPILE_VERSION = 3`, the record keeps `finish_reason`, `thinking_chars`, `cut`.
  Result: 0 → 10 compiled macros on the public rulebook; 9 still fail on SQL errors.
- `pipeline.route_reasons`: "changed rule" fired on 0/1000 questions (`pick_rules` lines carry a `[section] ` prefix).
  Fixed.
- `agent/calibrate.py` + CLI `shop-gate-fit` / `shop-gate`, `assets/shop_gate.json`: a shop above the train maximum of
  abstain rate (0.042) or raw-table rate (0.144) abstains entirely. Public B 0.406 → 0.553, train unchanged, but fragile:
  on public B merged with the 176-question rerun it gives 0.516 because dev_013 drops below the threshold. The signals
  were chosen after seeing which shops failed. Not ready for private.
- `remote/serve_vllm.sh` (vLLM Qwen3.5-9B as `llm`, TP=2, 64 seqs, port 18000) and `remote/run_parallel.sh` (prepare
  once, shop groups in parallel, merge). `run_parallel.sh` now balances groups on the `--ids-file` ids.
- Earlier today's `_samples` fallback (see the next section) is still uncommitted too.

**Targeted rerun (debug, 176 questions, `outputs/rented/delta_b`).** 151 wrong/abstain public B questions that touch new
rulebook lines plus 25 right controls: wrong group −105 → −91, controls 25/25 kept, merged public 0.406 → 0.420. Only
4/176 answers called a compiled macro (none right), so the gain comes from the router fix. Took 36.5 min, 23 of them
compile (serial, one request at a time); the compile cache now exists on the server.

**Known, not fixed:** `parse_macros` keeps duplicate names from one reply (the phone section repeated
`phone_model_lines` 12 times and hit `length` even without thinking); compile retries need a larger output budget.

**Server (vast.ai, `ssh -p 10351 root@45.143.122.52`; the key has a passphrase, ask the user, load with ssh-add).**
Code in `/workspace/zalo_ai` (rsync of `git ls-files` + `remote/` + `data/public_test` + `data/train/*.jsonl`,
then `uv sync --frozen`). vLLM runs from `remote/serve_vllm.sh` (log `/workspace/vllm_18000.log`). Runs go in tmux:
```bash
# on the server, after rsync of the code
remote/serve_vllm.sh --replace
BASE_URL=http://127.0.0.1:18000/v1 EXEC_WORKERS=4 remote/run_parallel.sh public_test outputs/rented/<name> 14 10 \
  --thinking route --max-tokens 2048 --catalog delta --judge unans --context rich --frame --rewrite residual \
  [--ids-file outputs/rented/<ids>.txt]
# on the Mac
rsync -az -e "ssh -p 10351" root@45.143.122.52:/workspace/zalo_ai/outputs/rented/ outputs/rented/
uv run datamate shop-gate outputs/rented/<name> --gate assets/shop_gate.json   # optional
```
Debug on selected ids, not the whole public test. A change that touches the compile key recompiles (about 21 min).

Pulled before the server was stopped (2026-11-14): `outputs/rented/` holds `public_b`, `delta_b` (submissions + logs),
`delta_ids.txt`, `compiled_public_test/`, `server_cache/` (the server's whole `cache/`: compiled, ingest, duckdb for
public_test, including the LLM verdicts and the COMPILE_VERSION 3 compile) and `server_logs/` (vLLM logs). Analysis
scripts (scorer by shop, rerun scorer, L8 gates, shop signals, store checks) are in `debug/rented/` (gitignored).
A new server needs the code, data and `server_cache/` copied back to `cache/` to skip the 21 min compile.

**Next (in order).**
1. L0: find train cases like the ingest removed in `7ca2324`; restore only those.
2. L8: code the four deterministic gates (train-measured).
3. L1: dedupe macro names per reply; larger output budget for the no-thinking retry; compile the train rulebook and
   compare with the hand macros.
4. Docker (L9); parallel per-shop prepare.
5. The user commits.

**Proposal after the measurements (2026-11-14, late, awaiting the user's review):** [docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md)
section 7. Short version: the 8 known-type public shops score 0.864 (v5: 0.848), the 6 new-type shops −0.408 with a
constant per-shop bias (dev_003 answers ≈ 1.04 × gold on 30/76 wrong numbers), so the risk unit is the shop, not the
question. Proposed: a gold-free shop audit at prepare time (P1; one flag, `status_in_text`, already measured 0/30 on
train), a shop × answer-type abstain policy (P2), an LLM store patch validated on mutated train shops (P3), compile
macros off by default (P4), three per-question gates measured on both train and public (P5), and a per-layer debug
guide (7.5). P1b (approved 2026-11-14): blank-cell fill per rulebook §8 misses the "id on every row, order fields on the
first row only" and "no id, blocks by date" shapes (dev_014 debt sheets); fix `_group_fill`, card note + lint for filled
order-level money columns, fixture tests; `fill_gap` audit flag is 0/30 on train. dev_009 shares its README sentence for the order-goods column with biz_110, so that role gap has a train
case.

**After public_c (2026-11-14, night):** PLAN_LAYERS section 8 reads the public_c logs (measure only) and the train route
logs. Of the 56 public wrongs, 6 come from the LLM rewrite turning `đh` into "doanh thu" (`đh` never occurs in train, so
it belongs in `lexicon_external.json`), 12 are unanswerable verdicts on customer-group or debt questions with no train
case (P), 18 are dev_012. Train's 16 wrongs left after the gates have mechanisms (guessed product code on a raw stock
table, frame entity missing from the SQL, shift percent). Order: Docker image built and run twice, lexicon `đh`, the two
lints, shift percent, then P3 only after the image is submitted.

## Status (2026-11-14): Qwen route runs on Kaggle (A/B public, train for L8)

**Gemma A/B of 2026-11-12 (read 2026-11-14, `outputs/kaggle/result_agents_{a,b}_gemma`).** Both ran only the `smoke`
stage (28 public questions, 2 per shop), not dev120 or public. Both ended in ERROR because the smoke gate failed
(`answered >= 25%: FAIL`), so `public` never started.

| run | smoke time | answered | right / wrong / abstain (`ground_truth.jsonl`) |
|---|---|---|---|
| A (`--catalog compiled --shop-views --judge unans`) | 6 h 31 | 6/28 | 6 / 0 / 22 |
| B (A + `--context rich`) | 6 h 45 | 4/28 | 2 / 2 / 24 |

Causes:
1. **Thinking ate the token budget.** In 22/28 (A) and 24/28 (B) questions, both attempts returned an empty reply
   (`No sql block or UNANSWERABLE line`). Each attempt took about 600 s at about 12–14 tokens/s per stream, which is
   about the `--max-tokens 8192` limit. The repair attempt reused the same budget, so a failed question cost about
   20 min. `ee9f9fa` did not log `finish_reason`, so this is inferred from token counts and timing: vLLM generated about
   614k tokens over 193 requests.
2. **Almost no parallelism.** `ThreadPoolExecutor(max_workers=cfg.concurrency)` (`pipeline.py`, around line 640)
   runs only the questions of one shop in parallel, and shops run one after another. Smoke has 2 questions per shop,
   so vLLM had 1–2 requests at a time (20–28 tokens/s in total). Public and train (70–120 questions per shop) are not
   affected as much.
3. **About 100 min of per-shop setup** (about 7 min per shop, mostly `--shop-views` with thinking), also one shop at a
   time.

Conclusion: Gemma 4 12B with thinking on every call, at 8192 tokens, cannot finish public in 12 h on T4, and would not
fit 6 h for private.

**Fix (uncommitted, in the working tree).** In `pipeline._samples`, a reply that is empty and cut at `length` makes the
next attempt use `llm.without_thinking()`. The empty turn is not sent back to the model. Test:
`tests/test_router.py::test_cut_thinking_retries_without_thinking`. 366 tests pass.

**Kaggle runs in flight (pushed 2026-11-14 about 02:00–02:30 VN).** All three use the same code: `1e9b3cf` plus the fix
above (BUILD src hash `44b0f19`). All run Qwen/Qwen3.5-9B with `DM_THINK=route`: thinking off by default, deep tier on
for hard questions with an 8192 budget. These are the user's choices: no dev120; A/B on public to measure; train for
L8 with the B config.

| account | kernel | stages | flags |
|---|---|---|---|
| hydrochii | `datamate-agents-a` (v3) | probe, smoke, public | `--catalog compiled --shop-views --judge unans` |
| hydrochii | `datamate-agents-b` (v1) | probe, smoke, public | `--catalog delta --judge unans --context rich --frame --rewrite residual` |
| hydrochiii | `datamate-route-train` (v1) | probe, train | the same flags as B (the calibrator must match the submitted config) |

- One account runs 2 GPU kernels at once: A and B were both RUNNING.
- A and B share the weekly GPU quota of `hydrochii`. About 7 h of it was already used by the Gemma runs, so two full
  12 h runs would exceed 30 h.
- Qwen3.5-9B speed with route thinking on T4 is not measured yet. Read `kaggle_run.txt` first (`probe:`, `stage smoke:`).
- If A wins on public, the train run (B config) does not match it: refit L8 on a new train run, or drop L8.

Push commands (from `text2sql`, working tree with the fix; `push.sh` rewrites `outputs/kaggle/{dataset,kernel}` and
`assets/BUILD.txt`, so push one at a time):
```bash
DM_KERNEL=datamate-agents-a DM_STAGES=probe,smoke,public DM_ENGINE=sql DM_THINK=route DM_MODEL=Qwen/Qwen3.5-9B \
  DM_ARGS="--catalog compiled --shop-views --judge unans" DM_DATA=datamate-bundle-combined DM_DATASET=datamate-code \
  kaggle/push.sh update
# B: DM_KERNEL=datamate-agents-b, DM_ARGS="--catalog delta --judge unans --context rich --frame --rewrite residual"
# train: KAGGLE_API_TOKEN=<token 2>, DM_KERNEL=datamate-route-train, DM_STAGES=probe,train, B's DM_ARGS
```

Read the results:
```bash
uvx --with six kaggle kernels output hydrochii/datamate-agents-a -p outputs/kaggle/result_agents_a_qwen
uvx --with six kaggle kernels output hydrochii/datamate-agents-b -p outputs/kaggle/result_agents_b_qwen
KAGGLE_API_TOKEN="$(grep '^KAGGLE_API_TOKEN_2=' .env | cut -d= -f2-)" \
  uvx --with six kaggle kernels output hydrochiii/datamate-route-train -p outputs/kaggle/result_route_train
```

**Next.**
1. Score A and B on `ground_truth.jsonl` (public measures only, never tunes) and check run time per 1000 questions.
2. Fit L8 on the train logs:
   `uv run datamate calibrate outputs/kaggle/result_route_train/out/train --out assets/calibrator.json`.
   Check `finish_reason`, the `route` tiers and how often the fallback fired.
3. Pick the final config and commit the fix (the user commits).
4. Docker (L9).
5. Open: parallelism across shops (smoke, private with many shops), per-shop setup time, an earlier smoke stop.

## Status (2026-11-13): layers of docs/PLAN_LAYERS.md (all but L9)

Code for the layers of [docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md), except Docker (L9). All new behaviour is behind flags,
so a run without them behaves as before. 365 tests pass. Nothing new has run on Kaggle yet.

| layer | what | flag / command |
|---|---|---|
| L1 | Rulebook diff against the train rulebook (`assets/rulebook_train.md`, allowed by QnA lines 29–30). `rules.rule_delta` keeps only new rules and rules the model reads as changed. `sql/compile.compile_delta` writes macros for those rules only. The tested macros of changed rules are blocked and their skeletons dropped. Lints, shadow and the fast branch stay as in `full`. The train delta is empty, so a train run makes no compile call; a public-style rulebook gives ≤ 28 rule lines. | `--catalog delta` |
| L1/L10 | Invariants without gold (`sql/invariants.py`). Hand macros: revenue adds up over days, cancel_rate in 0..100, cancel_count ≤ orders, debt ≥ 0. Compiled macros: same call gives the same value, NULL/0 after the data, a rate stays in 0..100; a violation goes back to the model like an error. Train stores: 0 violations. | `datamate rule-check --split train` |
| L5 | Thinking router. Thinking is off by default. A question goes deep with thinking (and the judge) when it reads a changed rule, the sales are suspect, the needed fact is suspect, it is unfamiliar (nearest train question of another shop < 0.75, about 12% of train), or it has ≥ 3 unexplained words. It escalates when the first answer abstained, was UNANSWERABLE, hedged, tripped a check or hit the token limit. `LocalLLM` returns `Reply` (str) with `.reasoning` and `.finish_reason`; both are logged per attempt (`thinking`, `finish_reason`). The log holds `route` {tier, reasons, agree, first}. | `--thinking route` (`--think-max-tokens` = deep budget, `--route-far`, `--route-residual`); Kaggle `DM_THINK=route` |
| L0/L2 | Shop capabilities (`capabilities.py`): revenue, orders, customers, products, debt, deliveries, imports, expenses, stock, lots, cost → trusted / suspect / absent, each with its source. Value checks of the facts (payments ≤ order total, delivery ≥ order date, imports amount ≈ qty × price, stock ≥ 0, lots have an expiry, expenses > 0) fire on no train shop. "No fact, but a raw header names it" → suspect, never absent. On train every question that needs an absent capability is gold-unanswerable (13/13), and no answerable question needs one. Used by the card under `--context rich` (replaces `absent_part`), the router, the example filter and L8. Saved as `<out>/capabilities.json`. LLM column mapping now records every proposal with the reason it was kept or refused: `cache/ingest/<split>/<biz>.roles.decisions.json`. | always (card: `--context rich`) |
| L4 | `agent/frame.py`: Frame (quantity from `lint.CATALOG` or a contract, periods, names, values, shape, capability, residual = words neither the slots nor the rulebook nor the shop's units explain). Logged as `frame` on every question. The example bank drops examples that need a capability the shop lacks. | `--frame` (block in the prompt) |
| L6 | `frame.consistency`: a resolved period or a name that the final query does not use (deterministic round trip, no model call), logged as `consistency`. Executed queries log `rows` (5 first rows were already in `stdout`). | always |
| L7 | `magnitude` = money answer / revenue over all the shop's data, logged as a soft feature. The contract kind gate was NOT added: in the train logs, 17/17 contract fallbacks were right (no train case; QnA rule). | always |
| L8 | `agent/calibrate.py`: features from the log (tier, reasons, agreement, repairs, checks, judge, macro calls, near, capability state, residual, consistency, magnitude, type). Logistic regression, leave-business-out report (reliability bins; PA by business, tier, capability, type), JSON model. Answers below the threshold abstain. | `datamate calibrate <train runs> --out assets/calibrator.json`; `--calibrator assets/calibrator.json` |
| L3 | Lexicon: entries kept only if they occur in train questions (29 words, 43 phrases; 109 words and 14 phrases dropped). External entries in `assets/lexicon_external.json` with their source: 16 from web pages (URL) and 67 from Gemini (train-only seed words, `preprocess/lexgen.py`, raw replies in `assets/lexicon_gemini_*.jsonl`). Every external entry passes `lexicon.accept` (no train word, no synonym of a business word), a Gemini check and a review list. They change no train question. LLM rewrite only when the rule-cleaned question leaves unexplained words (train: 1996 of 3000 would skip it, measured without the shop's name index). | `--rewrite residual`; rebuild: `uv run python -m datamate.preprocess.lexgen [--refilter\|--verify]` |

**L8 data (the question of 2026-11-13).** The calibrator must be fitted on train logs of the configuration that will be
submitted (same model, router, flags); public and private logs never. `hydrochii/datamate-sql-run` (SQL v4, code
`249b031`, Qwen, no thinking: train PA 0.8727, 2727/109/164) is being downloaded to `outputs/kaggle/result_sql_run`
(started 2026-11-13 ~00:55, about 1800/3000 logs at handoff; rerun the same command to resume). It can serve as a
prototype only: it has no router, frame or capability fields. `debug/l8_v4.py` (local, gitignored) fills those fields
from the old logs and runs the leave-business-out fit:
```bash
uvx --with six kaggle kernels output hydrochii/datamate-sql-run -p outputs/kaggle/result_sql_run --file-pattern "out/train/.*"
uv run python debug/l8_v4.py outputs/kaggle/result_sql_run/out/train outputs/l8_v4   # report + outputs/l8_v4/calibrator.json
uv run datamate export-examples outputs/kaggle/result_sql_run/out/train --out assets/examples_sql.jsonl  # L4 question → SQL bank
```
Check before using the bank: v4 queries may declare `-- type: number` for money; the run then picks it up automatically
(`examples_sql.jsonl` next to `--examples`).

**Next (user).** A Kaggle train run of the chosen configuration gives the real L8 data and measures the router. It runs
on the train split (3000 gold answers); public is for measuring only. The model must be the one submitted (Qwen3.5-9B,
or Gemma 4 12B after the A/B runs of 2026-11-12):
```bash
DM_KERNEL=datamate-route-train DM_STAGES=probe,train DM_ENGINE=sql DM_THINK=route DM_MODEL=Qwen/Qwen3.5-9B \
  DM_ARGS="--catalog delta --judge unans --context rich --frame --rewrite residual" \
  DM_DATA=datamate-bundle-combined DM_DATASET=datamate-code kaggle/push.sh update
```
Local checks: Ollama Qwen, 6 holdout questions, `--thinking route --catalog delta`: 5 right, 0 wrong, 1 abstain (the deep
one). With every new flag (`--context rich --frame --rewrite residual`, other 6 holdout questions, seed 1): 4 right, 1 wrong
(`q_e4fe38fbebb9`, a table, normal tier), 1 abstain. The abstain went deep on "residual 3" (`quay, zalo, oa`: the
channel values were not found by `values.hints`, so the frame could not explain them). 5 of 6 skipped the rewrite. Logs
were in the old session's scratchpad (lost).
Not done: fixtures per rule (L1), code-generated SQL for contract macros (L4), self-consistency in the deep tier and
per-shop skeletons (L5), checks turned from hard to soft (L7: needs the calibrator first), the `events` table (L0).

## Status (2026-11-12, night)

**Branches.**
- `text2sql` holds the code now running on Kaggle A/B: `ee9f9fa`, on top of `d14db4f` (`d14db4f` adds
  [docs/PLAN_LAYERS.md](docs/PLAN_LAYERS.md), the per-layer plan). `src/` is byte-identical to the pushed bundle, and
  `kaggle/run_kaggle.py` differs only in the values `push.sh` fills in.
- `submission` (`3b6df4c`, Docker `c27e90d`: vLLM v0.32.0-cu129, Qwen3.5-9B, `predict.sh`) has the same fixes. It also
  has the guideline log fields (`answer`, `elapsed_ms`, `thinking`, `code`, `output`) and no `--oracle`/`--keep-tables`
  or `ground_truth.jsonl`. Merging the two branches will conflict in `pipeline.py`, `sql/agent.py`, `run_kaggle.py`
  and the tests (`test_oracle.py` here, `test_pool_setup.py` there).

**Why A/B of 2026-11-11 gave 0/1000.** Neither cause was Qwen:
1. **Code.** A compiled macro, `date_trunc('month', as_of())`, crashed `SqlPool.setup`. DuckDB binds a table macro
   when it is created, and the pool's cursors had no `as_of`; only `usable()` set it on the probe. Compile and the
   first shop's views had already run for ~56 min. Fix: `setup` sets `as_of` to the data end on every cursor and
   skips a statement that still fails. The pipeline's `probe` cursor gets `as_of` too, so shop views are not
   rejected as empty without `--catalog compiled`.
2. **Gemma 4 on T4.** Worker log: `triton OutOfResources: shared memory, Required: 98304, Hardware limit: 65536` in
   `triton_unified_attention` during the cudagraph profiling run. The full-attention layers have head size 512, and the
   prefill KV tile of 32 needs 96 KB. This was not RAM or the KV cache. Fix (Kaggle only, `patch_attention()` in
   `kaggle/run_kaggle.py`, after the vLLM install): prefill tile 16 and `num_stages=1` for heads over 512 bytes;
   heads of 256 (Gemma sliding layers, Qwen) keep vLLM's choice. The 5090 has about 99 KB of shared memory, so the
   final image should not need it; untested.
   Also: Gemma runs with `--limit-mm-per-prompt '{"image":0,"audio":0,"video":0}'`. Qwen gets
   `--reasoning-parser qwen3`: without it, the 2026-11-11 probe had `reasoning 0 chars` and the thinking in `content`.

**New `smoke` stage** (`run_kaggle.py`): the first 2 questions of each shop (28 public questions), with every stage
of the run (compile, shop views, judge). A gate decides whether the long stages run: every line written, no
traceback, < 25% of questions with only LLM errors, ≥ 25% answered. On a fail the kernel stops before `public`.
`DM_MODEL` pins one model: no silent fall back to Qwen.

**Kaggle runs in flight (pushed 2026-11-12 ~21:08 VN, same code, Gemma 4 12B W4A16 `nicklas373/gemma-4-12B-it-AWQ`,
thinking on, `DM_STAGES=probe,smoke,public`):**

| run | kernel (version 2) | flags |
|---|---|---|
| A | `hydrochii/datamate-agents-a` | `--catalog compiled --shop-views --judge unans` |
| B | `hydrochiii/datamate-agents-b` | the same + `--context rich` |

Gemma served: the A log reached `stage smoke: start` at 701 s; the user saw it on the Kaggle page, since
`kaggle kernels logs` returns nothing while a kernel runs. Estimate (no Gemma throughput measured on T4 yet):
- smoke ends about 1.5–2.5 h after the start. A kernel that ends then means the smoke gate failed;
- public needs another 3.5–6 h, so A finishes about 04:00–06:00 VN on 2026-11-13;
- the 12 h cap is about 09:00 VN. The submission is written as it goes, so a capped run keeps its answers.

For scale, v5–v7 without thinking took 72–77 min per 1000 questions.

Read the results (the `kaggle_run.txt` lines `attention patch`, `probe:`, `smoke:`, `stage public:`), then score:
```bash
uvx --with six kaggle kernels output hydrochii/datamate-agents-a -p outputs/kaggle/result_agents_a_gemma
KAGGLE_API_TOKEN="$(grep '^KAGGLE_API_TOKEN_2=' .env | cut -d= -f2-)" \
  uvx --with six kaggle kernels output hydrochiii/datamate-agents-b -p outputs/kaggle/result_agents_b_gemma
```
Score with the snippet in the section below. `ground_truth.jsonl` is tracked here but untracked on `submission`:
keep a local copy before you check out `submission`. Check first: the `probe:` line shows reasoning chars > 0, and how long smoke and public took. That is the
first measure of Gemma's speed on T4, and the 6 h budget of the final image depends on it. B−A now measures
`--context rich` alone. Also look at the 13 public questions the "Not in" block risks (dev_014 debt/stock, dev_013
deliveries), as described below.

Push commands used (from `text2sql`):
```bash
DM_KERNEL=datamate-agents-a DM_STAGES=probe,smoke,public DM_ENGINE=sql DM_MODEL=nicklas373/gemma-4-12B-it-AWQ \
  DM_ARGS="--catalog compiled --shop-views --judge unans" DM_DATA=datamate-bundle-combined DM_DATASET=datamate-code \
  kaggle/push.sh update
# B: KAGGLE_API_TOKEN=<token 2>, DM_KERNEL=datamate-agents-b, DM_ARGS="... --context rich"
```

Previous runs downloaded to `outputs/kaggle/result_agents_a`, `result_agents_b` (0/1000) and `gemma_probe`.

## Status (2026-11-12, late)

Commits on `text2sql` (not pushed): `49a3930` agent pipeline (section below), `a8ddbae` `--context rich`, `0009102` and
`6064dfb` Kaggle runner fixes. `assets/oracle/` stays untracked (it was built from public errors).

**Kaggle runs in flight (public test, 1000 questions, SQL, Qwen3.5-9B with thinking, `--max-tokens 8192`):**

| run | account / kernel | code | flags |
|---|---|---|---|
| A | `hydrochii/datamate-agents-a` | `49a3930` | `--catalog compiled --shop-views --judge unans` |
| B | `hydrochiii/datamate-agents-b` | `6064dfb` | the same + `--context rich` |

A was meant for Gemma 4 12B but fell back to Qwen (see below); B pins Qwen (`DM_MODEL`) so the pair compares. B's code
differs from A's by more than `--context`: the compile lint (an argument named like a column is an error), shop views
that repeat a canonical table dropped, the judge on a doubt in any attempt. A B−A gap is all of these together.
Get and score them (public labels: evaluation only, never to pick a fix):
```bash
uvx --with six kaggle kernels output hydrochii/datamate-agents-a -p outputs/kaggle/result_agents_a
KAGGLE_API_TOKEN="$(grep '^KAGGLE_API_TOKEN_2=' .env | cut -d= -f2-)" \
  uvx --with six kaggle kernels output hydrochiii/datamate-agents-b -p outputs/kaggle/result_agents_b
uv run python -c "import sys; from pathlib import Path; from datamate.eval.scorer import read_jsonl, score
refs = {r['id']: r for r in read_jsonl(Path('ground_truth.jsonl'))}
for run in sys.argv[1:]:
    preds = {p['id']: p for p in read_jsonl(Path(run) / 'out/public/submission.jsonl')}
    print(run, {k: v for k, v in score(preds, refs).items() if not isinstance(v, dict)})
" outputs/kaggle/result_agents_a outputs/kaggle/result_agents_b
```
Compare with public v5 (leaderboard 0.759, replay 0.792, hand-written rule macros) to see what the agent direction
costs or gains. Per-question logs: `out/public/logs/<id>.json` (`judge` key: the second agent's verdict and action).

**`--context rich`** (commit `a8ddbae`; papers behind it: M-Schema/XiYan-SQL, Shkapenyuk 2025 profiling, BIRD
evidence +20 points, "Death of Schema Linking" (small models still gain from linking), DAIL-SQL, CHASE-SQL, TA-SQL, TrustSQL):
- card: a raw table that feeds sales_lines lists only the columns sales_lines lacks (`"Hàng" = product_name, ...`);
  a Keys block joins the other raw tables to the views; "Not in this shop's canonical tables" lists the facts the
  store lacks (payments/debt, deliveries, imports, expenses, stock, lots, cost); compiled macros show their SQL.
  biz_105 card: 13.5k → 10.3k chars. Risk to check on B's logs: a plain "shop has no X data" note was rejected on
  2026-11-08 (train 23/23 right, but public had 13 answerable questions whose facts ingest did not detect: dev_014
  debt/stock, dev_013 deliveries). The rich block adds "unless a raw table or a view written for this shop records it";
  look at those 13 questions in B before keeping it.
- [src/datamate/agent/evidence.py](src/datamate/agent/evidence.py): the rulebook lines a question's words point to
  (folded unigrams/bigrams, idf over the rulebook, term cell ×3, stop words), quoted next to the question.
- the query starts with `-- terms: ... -> column`, `-- rule: ...`, `-- needs: ...`.
- Not done (priority 4): a train bank of question → SQL pairs that matched gold, as few-shot (DAIL-SQL). Today the
  examples show answers only, and for a shop without debt data the debt examples of other shops pull toward answering.

Local check (Ollama `gemma4-12b-32k`, thinking off, 1–2 questions): the flow runs end to end. "số ly size M" right;
the debt question of biz_105 (gold unanswerable) answered with a table under `base` (−1) and abstained under `rich`
(0, by the walk-in check, not by the "Not in" block). With thinking off the 12B still invents "khách nợ -> orders"
and the compiled macros were sloppy (`payment_method = payment_method`, `is_valid_order` returning a count): thinking
is needed, and "runs" is a weak test of a compiled macro.

**Gemma 4 12B on Kaggle (T4, sm_75): not serving yet.** Found and fixed: the "AWQ" repo `nicklas373/gemma-4-12B-it-AWQ`
is compressed-tensors W4A16 (no `--quantization awq`); the nightly env held a torchaudio built for another torch, and
transformers imports it for a model with audio (now uninstalled when it cannot load; Gemma uses the stable env, vLLM
0.33.0 already registers `Gemma4UnifiedForConditionalGeneration` and the `gemma4` reasoning parser). Still open: the
W4A16 build loads for ~5 min, then "Engine core initialization failed"; the root cause is in the worker lines of
`vllm_gemma-4-12B-it-AWQ.log` in the probe's output (`hydrochiii/datamate-gemma-probe` version 2, `DM_STAGES=probe`):
```bash
KAGGLE_API_TOKEN="$(grep '^KAGGLE_API_TOKEN_2=' .env | cut -d= -f2-)" \
  uvx --with six kaggle kernels output hydrochiii/datamate-gemma-probe -p outputs/kaggle/gemma_probe
```
W4A16 kernels exist for sm_75 (Marlin and Humming ≥ 75, Exllama ≥ 60, Triton). Suspects: memory (vision/audio
encoders, 32k KV cache) or the Turing attention backend with Gemma 4's head sizes. Next tries: `--limit-mm-per-prompt
'{"image":0,"audio":0}'`, `--max-model-len 16384`, `--enforce-eager`. The runner now prints vLLM's error lines live.

Cleanup: stale DuckDB stores (other than `i21_s14`), stale compile/shop-view caches, smoke outputs, the dropped
pandas/codegen/combined Kaggle results (incl. E0 `result_train`), old SQL Kaggle results v3/v4/train_v2/dev120/oracle
and old local experiments were deleted. Kept: `cache/ingest`, current stores, `outputs/kaggle/result_sql_public_v5/v6`,
`result_public_v7`, `gemini_pilot`, `debug/`.

Next: score A and B; read the probe log and serve Gemma (then rerun A and B on Gemma: the user wants both on Gemma
with thinking, same code, only `--context` differing); the question → SQL example bank; Docker (user, pending).

## Status (2026-11-12): agent pipeline (mentor's direction)

Chosen by the user: the LLM reads the rulebook, writes the computations and decides (mentor's M1, M2, M4, M5);
hand-written rule code leaves the answering path. Gemma 4 12B with thinking (Qwen3.5-9B if Kaggle cannot serve it).
New run flags (SQL engine):
- `--catalog compiled` ([src/datamate/sql/compile.py](src/datamate/sql/compile.py)): at the start of a run the model
  reads the split's rulebook section by section and writes one DuckDB macro per definition. It gets the canonical
  schema (facts only, no rule) and the macros it already wrote. Each macro is called on every store of the split; one
  that runs nowhere goes back once with the error; a macro may not call the hand-written ones. Cached in
  `cache/compiled/<split>/` by rulebook, prompt version and model. Per shop, the macros that run are temp macros
  (they shadow stored ones of the same name); the hand-written rule macros are blocked, and the catalog advice, rule
  rewrites/lints, contract shadow check and fast branch are off, as in `primitives`.
- `--shop-views` ([src/datamate/sql/shopviews.py](src/datamate/sql/shopviews.py)): once per shop the model reads the
  README and the schema card and writes up to 6 views for what the canonical views do not hold (a kind of record of
  its own, a value packed in a text cell). A view must run and return rows; one repair round. Cached in
  `cache/shopviews/<split>/`. The canonical sales_lines/orders/facts stay (the user chose "bổ sung").
- `--judge unans|all` ([src/datamate/agent/judge.py](src/datamate/agent/judge.py)), the mentor's second agent: it
  reads the question, the shop part and the rulebook with the solver's verdict and says whether the data can answer.
  Agreement keeps the verdict; an answerable verdict on UNANSWERABLE gives the solver one more try with the judge's
  pointer; a disagreement that stays abstains. `all` also checks answers (an answer the judge finds unsupported abstains).
- `--prepare-only`: run the compiler and shop views, answer nothing; `--think-max-tokens` (8192): their budget.

Validation (user's choice): `assets/holdout.txt`, 80 questions (16 per shop, every answer type) of five train shops
of different export styles: biz_105 (hand-kept book), biz_110 (orders only), biz_123 (sqlite POS), biz_124 (mixed
sources), biz_128 (pharmacy). They are never used to pick prompts or fixes. Public only measures; a fix needs a train case.

Kaggle: `kaggle/run_kaggle.py` tries Gemma 4 12B first (the W4A16 build in fp16, then the full model; T4 has no
bf16), then Qwen3.5-9B. Thinking is on for every model. (Superseded on vLLM env and quantization: see the late status above.)
`DM_STAGES=holdout,public DM_ENGINE=sql DM_ARGS="--catalog compiled --shop-views --judge unans" kaggle/push.sh update`.

---

## Status (2026-11-11, late): public knowledge removed

The organizers' answers (`description/QnA.txt`) say the public test is used neither to train nor at inference, "kể cả
việc xử lý tập public test để tạo ra thông tin train khác", and only the train rulebook may be packaged. Removed:
- `rules.TESTED`: the public wording of 'Đơn thuộc kỳ nào';
- macros for public-rulebook terms: `hour_stats`, `model_line`, `brand_of`, `extra_lines`, and the columns `days`,
  `avg_order`, `cancels` (customer_stats), `orders` (day_stats), `days` (group_stats); their catalog regexes and docs;
- ingest built from public shops: conversion rules written in a cell, the abbreviation lookup sheet, staff full names,
  toppings in a drink's cell, status read from notes, ticket rows without an order id, the README "các dòng cùng
  **X** là một đơn" key, payments read from order notes, loose customer-group headers;
- header and status alternatives that match public headers only (checked against every train header);
- public-only words in lints and value hints; `tests/test_public_fixes.py` (public questions).

Check: the canonical sales lines, orders and fact tables of all 30 train shops are identical before and after
(the scratchpad `train_snap.py`), so everything removed served public only. `INGEST_VERSION` 21, `STORE_VERSION` 14.
Public scores from before this change are no longer comparable.

---

## Status (2026-11-11): start here

**Public v5** (`hydrochii/datamate-sql-public`, code `fd767ea`), output in `outputs/kaggle/result_sql_public_v5`:
- leaderboard **0.759**; by labels 0.751 (836 right / 85 wrong / 79 abstain);
- error groups in `debug/sql_err_public_v5/REPORT.md` (gitignored).

**Committed on `text2sql`** (`b89d4dd`..`5de216b`, not pushed): train rules as macros, fixes the code can make, value hints.
`STORE_VERSION` 13, 367 tests pass. Old stores (`i19_s8`, `i19_s9`, `i20_s11`) were deleted.
A fake-LLM smoke run had written `{}` roles for public dev_002/003/008 and a rule verdict; both were removed.

New macros, each gated on its train-rulebook clause (`rules.DEPENDS`/`TESTED`):
- money: `revenue_of`, `share_of`, `orders_of`, `qty_of`, `group_stats`, `flag`;
- months and units: `revenue_rising`, `qty_unit`;
- imports and stock: `import_total`, `supplier_stats`, `out_of_stock`;
- debt: `debt_age_sum(x, lo, hi)` replaces `debt_aging`.

Rulebook parameters:
- the shift hours are read from the rulebook text (`rules.rule_params`) and set per session; if they cannot be read, the shift macros are off.
- a clause that is missing from the rulebook turns its macro off. On public this switches off `price_on`, `avg_import_price` and `stock_split`.
- the reworded 'Giao nhiều đợt' clause is asked once to the model.

Code fixes:
- `rewrite_macros`: a table macro read as `m(...).col`, a percent macro scaled ×100 or divided by a count, `price_effect` given a price;
- `free_text_lint`: `=` on a free-text note column;
- `unit_shadow`: 'bao nhiêu <đơn vị>' is checked against `qty_unit` after the query runs;
- `_written_rules`: a conversion written in the cell ('thùng = 4 bịch');
- a supplier column in the imports fact;
- `out_of_stock` uses an alias, so `stock_at(product_name, …)` in a WHERE does not bind to its own table.

`sql/values.py`: deterministic value hints, no model call:
- the column and the exact value behind a question phrase (with synonyms such as tiền mặt → cash, tại quầy → store);
- 'nhóm X' that no column holds;
- a subjective superlative the rulebook does not define.

**Measured:**
- `sql-check` and `facts-check`: no misses;
- new macros against train gold: rising 21/21, supplier 7/7, import_total 22/22, out_of_stock 1/1, kê đơn 38/38, unit counts without an item 9/9, channel / payment / group 215 right;
- replay of logged replies (`debug/replay_v6.py`):
  - train without catalog advice: 0.9037 → **0.9077** (+6, 0 lost);
  - public v5: 0.751 → **0.792** (+34 questions, 0 lost).
- caveats:
  - the public gain comes from fixes made after reading public errors;
  - the new macros and value hints only act through new model replies, so they need a live run.

**Still wrong after replay:**
- public: 140 questions, 208 points lost (groups in `debug/sql_err_public_v5/labels_v5.txt`).
- train: 177 questions, 277 points lost. Its logs predate the formula catalog, so most of these groups now have a
  macro that the logs never saw.
- next step: a live run, on dev120 or public.

## Status (2026-11-08, end of day)

**Where to work.** One folder, `zalo_ai`, on branch `text2sql`. `main` is the final code and takes pull requests from `text2sql`.
Everything below is **committed** on `text2sql` (`4dd9655`..`fd767ea`, 8 commits). Nothing is pushed to GitHub; opening the PR is the user's call.
The user's own uncommitted changes (`ground_truth.json` and `ground_submission` deleted, `ground_truth.jsonl` untracked) are left alone.

**Commits** (the first four are fixes 1–3 of the section below):

| commit | content |
|---|---|
| `4dd9655` fix(ingest) | customer group, `base_unit`, product codes, order columns, debt from notes, price list; `INGEST_VERSION` 20 |
| `160cb28` fix(preprocess) | 'từ mùng 1', month lists, Northern dish words, slang |
| `28f23d9` fix(answer) | money rounded half up; `money_unit` skipped on amount filters; revenue contract off for counts |
| `29fe362` feat(sql) | formula catalog: macros + one-rewrite advice; order-money and scalar-macro lints; prompt examples use placeholders |
| `1a1a89d` feat(sql) | `stock_split`, `stock_value` (+ `Facts.catalog_cost`); `STORE_VERSION` 11 |
| `71e5e77` fix(hints) | 'N tháng liền nhau kết thúc ở tháng X' lists all N months |
| `4775f9c` fix(answer) | whole/loose stock ('mấy hộp chẵn, mấy viên lẻ') is a list of exactly 2, else a hard re-check |
| `fd767ea` fix(rewrite) | a rewrite that changes a business word (ca sáng/chiều, chẵn/lẻ, mọi/mỗi, bounds, status, size) is dropped |

**Where the new fixes came from.** Six hard train questions were traced live with local Qwen (`debug/example_io/q_*.md`: full prompt,
replies, execution). The questions covered missing stock data, whole/loose stock, stock value, a run of 3 months, net profit and COD vs cash.
Before the fixes, 3/6 were right. Ollama/MLX and Kaggle vLLM decode differently, so one local run is no measurement.
- `stock_value`: train gold 5/5; the one shop without it is gold-unanswerable. `stock_split`: 16/16; 3 shops without it are
  unanswerable. Scripts: `debug/fix4/oracle_stock_{value,split}.py`.
- Month runs: the time-hint diff changes only those questions (train 7/3000, public 1/1000).
- Rewrite check, against the train logs: 12/2935 rewrites are rejected (6 lost 'ca chiều', 4 wrote 'chính' for 'chẵn'). `debug/fix4/rewrite_markers.py`.

**Rejected: a "this shop has no X data" note in the schema card.** On train it separates perfectly: 23/23 questions in shops
without a fact are unanswerable, and 318/318 in shops with it are answerable. On public it fails: 13 questions in shops where the fact
detector found nothing are answerable (dev_014 debt 6 and stock 2, dev_013 deliveries 3, lots 2), and none are unanswerable.
The detector misses new formats, so the note would push wrong UNANSWERABLEs. Script: `debug/fix4/missing_facts.py`.
Better fact detection for those formats comes first.

**Design note from the user (2026-11-08).** Keep two responsibilities apart:
- question normalization keeps the content and only fixes the wording;
- intent and semantic extraction finds the object, measure, unit, time, operation and output shape.

Normalization must never decide business logic. Today one LLM call does both. It returns the `normalized` text plus
`answer_type`/`top_k`/`sub_questions`, and the LLM fields are only soft hints under the deterministic rules. `fd767ea` enforces the
normalization half. A clean split, with normalization only and then a separate intent frame (deterministic first, LLM for the rest), is the next design step.

**Measured after all commits:**
- 356 tests pass;
- `sql-check` and `facts-check`: no misses;
- A1 oracle: 28/28; catalog oracle: 68/69;
- replay: train 0.9037, public 0.655, both unchanged. The new fixes act only through new model replies, which replay cannot make.

**Kaggle run in flight:** kernel `hydrochii/datamate-sql-public`, dataset `hydrochii/datamate-bundle-sql`, `DM_STAGES=public`,
`DM_ENGINE=sql`, code `fd767ea`. Download it with `kaggle/fetch_output.py` into `outputs/kaggle/result_sql_public_v5`. Score it with
`ground_truth.jsonl` (evaluation only), and compare with the combined run's SQL arm (public 0.597 by labels) and with replay 0.655.
Submitting to the leaderboard is the user's call.

**Next:**
1. Read the Kaggle run:
   - PA by labels, known industries vs new;
   - time per question (one local stock-value question took 1038 s; find out why);
   - how often catalog advice fired and was followed.
2. Split normalization from intent extraction (design note above).
3. Fact detection for the new formats that `missing_facts.py` found: installments as debt, deliveries in dev_013. Hold dev_012
   and dev_013 out of further new-industry work, so they keep an honest estimate.
4. Make `sql` the default engine; Docker; deadline 2026-11-15.

## Status (2026-11-08, late night)

**Where to work.** One folder, `zalo_ai`; the worktree `../zalo_ai_text2sql` is removed. Develop on `text2sql` and switch to
`main` with `git checkout`. `main` holds the final code and takes pull requests from `text2sql`. The work below was
committed at end of day (see above).

**Step 0: public error attribution** (labels for evaluation only). The report is `debug/sql_err_public/REPORT.md` (gitignored), with
253 labelled questions in `labels.tsv`, put in stages S1 data, S2 reading, S3 query and S4 decide, and in remedy buckets A1–A5, B, C, D and E.
`replay.py` replays the logged public replies against `ground_truth.jsonl`. A question that needs a new reply counts as an abstain,
so replay gives a lower bound. Labels have no aliases, so a name answered without its phone number counts as wrong locally.

**Fix 1: store, facts and guards.** These changes are deterministic. `STORE_VERSION` 10, `INGEST_VERSION` 20, `SEMANTIC_VERSION` unchanged.
- `canon`: the customer group column is found by header and value shape (`customer_group_col`). The `base_unit` column comes from a role, a
  base-unit header, a master with one row per product, or a sold unit that has no conversion row. Product codes come from a master's code column,
  so conversion tables keyed by code now join. `orders` keeps the role columns plus only columns that are constant within an order.
  `_NOT_MASTER` no longer matches "TongHop".
- `facts`: expense headers are wider. Payments come from order notes when there is no payments table (`note_payments`):
  "đưa trước X, còn lại ghi sổ", "nợ X", and a debt paid up later. The price list comes from a price table (`Facts.price_list`).
- Guards: `fit_money` rounds a fractional money answer half up; no money gold in train or public is fractional. `money_unit` is skipped
  when the question names an amount. The revenue contract no longer matches count questions. The new `order_money_lint` flags summing
  order-level money over lines. The new scalar-macro lint flags `revenue()` with FILTER/FROM/WHERE/OVER.
- Reading: "từ mùng 1" means this month. A bare month in a list takes the year of a later dated month. Northern dish words map to
  southern ones (lợn/heo, rán/chiên, ngao/nghêu, đậu phụ/hũ…). Slang added: mí, tổg, cộg, đơng, xi ô đi, cháy hàng. "kiểm tra lại" is no return.

**Fix 2: formula catalog.** These are store macros: `customer_stats`, `item_stats`, `day_stats`, `month_stats`, shift/hour, customers or
items with at least N orders, group AOV, `size_qty`, `model_line`/`brand_of`, `extra_lines`, `price_increase_day`/`price_effect_at`,
`debt_aging`/`debt_customers`, `import_minus_sales` and `expiring_lots`. Each is created only where the shop's data has what it needs.
`agent/lint.CATALOG` maps question shapes to them, and `term_advice` asks for one rewrite with that macro.
On gold, with arguments parsed from the question (`debug/sql_err_public/catalog_train.py`, `oracle_catalog.py`):
- train: every parsed question is exact (customers ≥N orders 29/29, items 18/18, days 12/12, months 16/16, shift 77/77, size 46/46);
- public: 68/69, and the one miss is the phone-in-name label.

**Fix 3: new industries.** Only mechanisms that transfer to other shops: price list, `base_unit`, size and model from the name, extra
lines (voucher/ship/points), debt from notes. A global abstention rule was not adopted. Raw-table-only queries have precision 0.39 in the
new industries but 0.71–0.87 in known ones. Linting raw sales tables would touch 29 right train answers.

**Measured** (logged replies replayed through the new code):
- tests: 352 pass;
- `sql-check` and `facts-check`: no misses;
- A1 join oracle: 28/28;
- train replay: **0.9037**, the same as before; no question changed;
- public replay: 0.608 (step 0) → **0.655**. Against the previous replay, 10 wrong→right, 5 wrong→abstain and 1 right→abstain.
  The right→abstain one (q_701bf339bd9e) needs a new reply. Catalog advice and the new lints only pay off in a live run.

**Not done:**
- repair: plate/model split;
- installments;
- main supplier ("NCC chính");
- delivered quantity;
- `extra_lines` ship for one month: 4.80M vs gold 4.825M.

**Next**:
1. A live model run to measure what the catalog and advice add, either on the local dev subset or with the Kaggle SQL arm (`DM_ENGINE=sql`).
2. Make `sql` the default engine.
3. Docker. Deadline 2026-11-15.

## Status (2026-11-08, night)

**Where to work (superseded above).** Branch `text2sql`, in the worktree `../zalo_ai_text2sql`. Commit there and open pull requests to `main`;
do not commit to `main` directly. `main` already has PR #4 (the evening merge). The `combined` worktree is removed;
`codegen` and `combined` exist only as branches (local and on `origin`), as history.

**Error analysis of the SQL arm on train** (combined run above, 219 questions not right). The reports are in `debug/`,
which is gitignored, so they exist only in this worktree:
- `debug/sql_train_errors_code.txt`: 49 questions (11 wrong, 38 abstain) caused by code. C1–C11 are listed with mechanism, proof and fix; C12–C13 are minor.
- `debug/sql_train_errors_model.txt`: 139 questions (94 wrong, 45 abstain) caused by model behaviour, in 21 groups (M1–M21), each with possible directions. **The user decides these.**
- `debug/sql_train_unanswerable.txt`: the 70 gold-unanswerable questions by reason, and what each layer did on the 42 that fail.
  The reasons: 47 missing data, 16 period before the data, 6 subjective, 1 external knowledge.
- Scripts and data: `debug/sql_err/`. `extract.py` and `groups.py` hold the classification and `write_files.py` writes the reports; `q.py` queries a train store.
  `replay.py` replays the logged SQL replies through the current code. A question that would need a new reply counts as an abstain.

**Code errors fixed** (commit on `text2sql`; `STORE_VERSION` 9). None is keyed on a question or a business (rule 4); each
reads content (columns, headers, rulebook, wording):
- C1 "tháng nào": a month answered as 'YYYY-MM-01' becomes 'YYYY-MM' (`postprocess.fit_month`). A 'YYYY-MM' cell stays a month.
  Check messages name SQL types (`sql_terms`) instead of `answer_month()`.
- C2 the pandas "helper" guard no longer refuses SQL unanswerables. It matched the word `sales_lines` and blocked 8 right ones.
- C3 the "empty" guard accepts an unanswerable when a resolved period lies outside the sales data (`periods_outside`, as `revenue()` tests it).
- C4 a type only the LLM rewrite expects no longer sends a right answer back (train net −11 before).
- C5 0 from a bare `price_effect` / `count(*) from slow_products` call is an answer, not a filter bug.
- C6 "Tết năm nay / năm trước" → `tet(year)` / `tet(year-1)`. Before, it gave next year's Tết plus `ytd()`.
- C7 `facts._lots`: a lot list in base units wins over a hand-kept import book in import units, which only lends its price (biz_124 family).
- C8 no `gross_profit()` advice for per-product margin in SQL. The macro is shop-wide.
- C9 `fold_text` treats `_` as a space. `same_text` also ignores punctuation ('zalo_oa' = 'Zalo OA'). `has_name` keeps punctuation ('(M)').
- C10 grouping `sales_lines` by product adds `product_name is not null`. A service-fee line is no item.
- C11 the prompt's product-group example uses `same_text` (rulebook: exactly that group).
- C12 no `not walkin` rewrite or lint in a shop without customers. C13 the 'hoạt chất' lint needs a non-empty name.
- Checks: 334 tests pass; `sql-check` and `facts-check` have no misses.
- Replay on train: 0.8893 → **0.9037**. Changes: 28 abstain→right, 8 wrong→right, 1 abstain→wrong (q_1a79715010d5, which the removed soft check used to stop).
  C6, C11 and part of C1 need new replies, so a real run gains more or less than the replay.

**Next**:
1. The user picks directions from `debug/sql_train_errors_model.txt` and the unanswerable report. Candidates already
   checked on gold:
   - `base_unit` column (units sold);
   - size from the name;
   - `stock_split` macro (pandas has it, SQL not);
   - `stock_value` macro;
   - price-increase day from the price list;
   - lint for `revenue()` with FROM/WHERE;
   - gate on missing facts and on periods outside the data.
2. Replay public with the labels (analysis only), then a Kaggle run of the SQL arm alone (`DM_ENGINE=sql`) for the real score and the time per question.
3. Make `sql` the default engine. Docker is still deferred; deadline 2026-11-15.
4. Still open from the evening list: count guard on the contract fallback, "mí đơn" normalization, new-industry definitions.

## Status (2026-11-08, evening)

**Decision (the user, 2026-11-08):** text-to-SQL only. The `codegen` (pandas) and `combined` (both arms) directions are
dropped: the combination scored highest, but it doubles the pipeline, and SQL is ahead of pandas on train.
- Both branches stay on `origin` as they are; their sections below are history.
- `text2sql` is merged into `main` (local only, not pushed). Work continues on `main`.

**`text2sql` now holds the SQL commits of `combined`** (cherry-picked):
- `a83ba5f` `status__norm` asked of a view;
- `ecd8cf5` toppings in a drink's cell, `Phí phục vụ` not an item (`STORE_VERSION` 8);
- `e8b8f96` `kaggle/fetch_output.py`.
- `src/datamate/sql/`, `canon.py` and `store.py` are now identical to `combined`. What differs is pandas or `--engine both` code only.
- 320 tests pass; `sql-check` has no misses.
- The default engine of `datamate run` and `kaggle/run_kaggle.py` is still `pandas`: pass `--engine sql` / `DM_ENGINE=sql`.

**Last Kaggle run** (`combined`, code `124709f`, `src` hash `daa2a42`, checked in `out/BUILD.txt`).
Saved in `outputs/kaggle/result_combined_{train,public}`. Public is scored with the labels, not submitted.

| | train | public (labels) |
|---|---|---|
| combined (vote) | 0.9053 (2771/55/174) | 0.636 (742/106/152) |
| **SQL arm** | **0.8893** (2781/113/106) | **0.597** (735/138/127) |
| pandas arm | 0.8827 (2768/120/112) | 0.582 (739/157/104) |
| wall time, 2× T4, both arms | 19 243 s (6.4 s/question) | 8 880 s (8.9 s/question) |

- The SQL arm ran the same SQL code as `text2sql` head, so it is the current estimate for this branch: train 0.889, public about 0.60.
- In this run the arms share one preprocess. The SQL arm's answers are read from `arms.sql` in each log.
- Public, SQL arm: known industries 0.701 (790 questions), new industries dev_011–014 0.205 (210 questions, 167 points lost).

**Offline replay vs Kaggle.**
- Each arm did better on Kaggle than its replay: SQL 0.8893 vs 0.8847, pandas 0.8827 vs 0.8773.
- The combination did worse: 0.9053 vs 0.9147 on train, 0.636 vs 0.651 on public.
- Cause: the replay counts a question that needs a new reply as an abstain. That made 141 train questions "SQL only" (89% right). Fresh pandas replies answered them and often disagreed.
- Not the cause: the shared preprocess. Its text equals the old runs' text on 2989/3000 (SQL) and 2991/3000 (pandas) train questions.
- Lesson: the replay is fine for one arm and misleading for a combination.

**SQL arm errors found in this run** (verified on logs, not fixed):
- **Month answers, all abstain** (train 16/16, public 12/13, "tháng nào doanh thu cao nhất").
  - The query returns `'2025-08'`; the result is coerced to the date `'2025-08-01'`.
  - The check then asks for `answer_month(year, month)`, which does not exist in SQL (Catalog Error when the model tries it).
- **Unanswerable answered** (public: 30 references, SQL right on 9, answered 7; train 70 / 28 / 8):
  - "nhóm X" when the shop has no product-group column: the model matches product names (dev_002, dev_004).
  - Subjective words the rulebook does not define: khách trung thực / thân thiện / khó tính nhất, NCC uy tín / đáng tin cậy.
- **Contract fallback replaced a right count:** "bao nhiêu đơn có doanh thu từ 410.000" 846 → 858 023 000 (monthly revenue), because the question says "doanh thu".
  Elsewhere the fallback helps (pandas train +12, public +7), so it needs a guard for counts, not removal.
- **Preprocess:** "mí đơn tổg cộg" (= mấy đơn tổng cộng) is not normalized; the model answered revenue instead of an order count.
- **Weakest SQL intents on train** (points lost): stock as `[chẵn, lẻ]` 26 (PA −0.37, SQL returns one number), shifts 25, stock 25, drink size M/L 24, units sold ("mấy ly/gói/m3") 24.
  On public (known industries): units sold 23 (PA 0.26), price-change effect 12, customer ranking by money 14 and by frequency 13.
- Still open from the previous section: `orders` grain, price-change date, new-industry definitions, `has_name` substrings, the "đồng implausibly small" invariant.

**Outputs cleaned** (2026-11-08, 362 MB → 175 MB):
- Deleted: `outputs/kaggle/dataset` and `kernel` (rebuilt by `push.sh`), `kaggle/result2` (log of a broken run), `kaggle/result_codegen_run1` (kernel log only), `results_v8.zip` (same logs as `kaggle/result_train`), `smoke_dummy`, and the progress-bar logs `dev120_v1.log`, `dev120_v2.log`, `e1_off.log`, `wrong_v3.log`.
- Kept everything else, pandas runs included: they are baselines and paired controls.

**Next** (SQL only):
1. Fix month answers in SQL (format `YYYY-MM` when the expected answer is a month), the count guard on the contract fallback, and the "mí đơn" normalization. Measure each with `debug/replay_sql.py` on train and public.
2. Unanswerable: undefined subjective superlatives and missing group columns → `unanswerable`, gated on the rulebook and the schema, not on wording alone.
3. The grain and new-industry items listed below.
4. Make `sql` the default engine, then a Kaggle run of the SQL arm alone to get its time per question (about 4.3 s on 2× T4 before).
5. Docker is still deferred. Deadline 2026-11-15.

## Status (2026-11-08)

The same section is in the README of all three branches. Each branch has its own worktree. Nothing was pushed to GitHub at the time.

| direction | branch | worktree | head | last Kaggle run | head, offline replay |
|---|---|---|---|---|---|
| pandas codegen | `codegen` | `zalo_ai` | `07f9e96` (+ this docs commit) | `28acb2a`: train 0.8333, public LB **0.554** | train 0.8753, public 0.565 |
| text-to-SQL | `text2sql` | `../zalo_ai_text2sql` | `3903322` (+ this docs commit) | `249b031` (`datamate-sql-run` v4): train 0.8727, dev120 0.8583, public LB **0.567** | train 0.8820, public 0.590 |
| **combined** (both arms) | `combined` | `../zalo_ai_combined` | `6a77439` (+ this docs commit) | running, see below | train **0.9147**, public **0.651** |

**How the numbers were measured.**
- *Offline replay:* the model replies logged in the last Kaggle run go through the head code without calling the model. A question that would need a new reply counts as an abstain.
- Scripts: `debug/replay_sql.py` (in the `text2sql` and `combined` worktrees) and `debug/replay_pandas.py` (in `codegen` and `combined`). Both are gitignored.
- Controls: the old code replayed the same way reproduced Kaggle on 4000/4000 questions for SQL and 3998/4000 for pandas.
- Prompt changes cannot be measured this way.
- *Public numbers other than LB* are scored with `ground_truth.json` from `origin/main`, analysis only. Read it with `git show origin/main:ground_truth.json` into a scratch directory; never merge it.
  - It agrees with the leaderboard within 0.01.
  - It has no aliases, so names with phone numbers score as wrong when they are not.

**Branch `combined`** (from `3903322`):
- `272ca20` the codegen pandas fixes (dm `Period`, `rank_top`, mask lines, `mask_advice`, prompt example).
- `04e4733` `datamate run --engine both`:
  - one shared preprocess, then a SQL arm and a pandas arm per question;
  - `vote.combine` returns the shared answer when the arms agree, the only answer when the other abstains, and abstains when they disagree. Tables are compared by rows only;
  - each arm's samples and final are logged under `arms`.
- `48a956c` `status__norm` on a view:
  - the prompt line said every status has `<col>__norm`; the views call it `status`;
  - the lint did not see `status__norm`;
  - `status__norm` is now renamed to `status`.
- `124709f` canon (`STORE_VERSION` 8):
  - toppings written in a drink's cell become topping lines (`split_toppings`);
  - `Phí phục vụ` lines keep their amount and `counted` but lose product name and quantity (`drop_fee_items`). Rulebook: still revenue, not an item.
- `6a77439` `kaggle/fetch_output.py`: downloads every output file of a kernel, paged and in parallel.
- Checks: 328 tests; `sql-check` and `semantic-check` 139/139; `facts-check` no misses.
- Offline per arm on this branch: SQL train 0.8847 / public 0.598; pandas 0.8773 / 0.575; combined 0.9147 / 0.651.
  - Combined minus SQL: train +0.030, CI [0.021, 0.039]; public +0.053, CI [0.027, 0.080].
  - Public known industries 0.762, new industries 0.233.

**Kaggle runs in flight.**
- Pushed 2026-11-08 with `DM_ENGINE=both`.
- Code = `124709f`: the `src` hash in `out/BUILD.txt` is `daa2a42`, and line 2 shows `3903322` plus the then-uncommitted files.

| account | kernel | stage | dataset | expected on 2× T4 |
|---|---|---|---|---|
| `hydrochii` | `datamate-combined-public` | public | `datamate-bundle-combined` | about 3 h |
| `hydrochiii` | `datamate-combined-train` | train | `datamate-bundle-combined` | about 5.5 h |

```bash
cd ../zalo_ai_combined
uvx --with six kaggle kernels status hydrochii/datamate-combined-public
uvx --with six --with requests --from kaggle python -I kaggle/fetch_output.py hydrochii/datamate-combined-public ../zalo_ai/outputs/kaggle/result_combined_public
# second account: set the token for that command only, never print it
KAGGLE_API_TOKEN="$(grep '^KAGGLE_API_TOKEN_2=' ../zalo_ai/.env | cut -d= -f2-)" uvx --with six --with requests --from kaggle python -I kaggle/fetch_output.py hydrochiii/datamate-combined-train ../zalo_ai/outputs/kaggle/result_combined_train
```

**Read this session.**
- SQL v4 is saved in `outputs/kaggle/result_sql_v4`.
- `dev120_oracle` scored 0.8083, which is −0.05 against dev120, so table selection is dropped (the team's rule).

**Analysis for the mentor.**
- `docs/PROPOSAL_V4.md` is in the `codegen` worktree, uncommitted; the user edits it. It covers both flows, 9 layers each, with input/output and table-level error examples.
- `debug/analyze_v4.py` (gitignored) writes `outputs/analysis_v4/`.

**Errors verified on the data, not fixed yet** (layer numbers as in the proposal):
- **L3, grain:**
  - The `orders` view keeps line attributes (`Loại`, `Nhóm hàng hóa`) from the order's first line only. dev_001: 82 instead of 229.
  - Order-level money (`Khách trả`, `Tổng thanh toán`) sits on every line and gets summed per line. dev_011: ×2.
- **L3, price changes:** no way to find the date of a price change. The model passes today to `price_effect` (−95% instead of −3.6). The date is in the price-history table.
- **L3, new industries:** no definitions for installments, warranty tickets, mechanic commission, repair-shop revenue. New industries score 0.19.
- **L3, `has_name`:** it matches substrings ('trà' catches 'trà sữa') and misses variants ('vá ruột' vs 'vá', 'vá xe').
- **L2, debt in free text:** debt written only in a notes column (dev_008 `Note`: "nợ 79k, khách ck đủ rồi").
- **L4, generic names:** a generic name resolves to one variant ("iPhone 13" → 'iPhone 13 128GB Trắng'). The pandas `find()[0]` does the same.
- **L5, rulebook:** given in full, so definitions are missed ("thợ chăm chỉ nhất" = most working days, not most tickets).
- **L6:** the model reads `raw_*` instead of the normalized view (mechanic nicknames in `raw_sosuaxe`), and does not recognize unanswerable questions (train 55 points lost).
- **L8, checks:**
  - The invariant "đồng implausibly small" fires on counts whose question mentions an amount.
  - The contract fallback replaced a right order count (846) with monthly revenue, because the question said "doanh thu".

**Next.**
1. When the kernels end: download, check `BUILD.txt`, score.
   - Compare each arm with the offline figures (SQL 0.885 / 0.598, pandas 0.877 / 0.575, combined 0.915 / 0.651). The arm answers are in the logs under `arms`.
   - `datamate compare` against `result_sql_v4` and `result_codegen_v3`.
2. The public submission is the user's call. Label scorings are limited.
3. Fix the open errors above in that order: grain first, then price-change date, rulebook clause per question, and the two check rules. Measure each fix on train and public with the replays before a Kaggle run.
4. Time: both arms take about 9.5 s per question on 2× T4 (SQL 4.3 + pandas 5.2). Measure on 2× 5090 against the 6-hour limit. Docker is still deferred. Deadline 2026-11-15.

## Status (2026-11-07)

**Results of the 2026-11-06 runs (code `4abdef9`, ingest 12).** Public SQL **0.356**. Train SQL 0.687 (2385 / 323 / 292), train pandas 0.669 (2299 / 291 / 410),
ΔPA +0.018, CI [−0.006, 0.044], so G1 has no clear winner: the arms fail in different places. Team decision: keep both arms for now.
Per-question train errors: `outputs/train_errors_sql_pandas.csv`.

**Decisions.** Only train feeds the solution. Public structure read from READMEs and rulebooks may guide generic code, tested on train; public labels are for
analysis and never enter the code or the image. The goal is generalization to private. Directions 2 (answer on agreement) and 10 (fine-tune) are pending.

**Built on 2026-11-07.** Committed on both branches, shared code identical, `INGEST_VERSION` 18, `SEMANTIC_VERSION` 2026-11-07.2, `STORE_VERSION` 7.

| item | where |
|---|---|
| Fact tables and helpers: debt, payments, trips, imports, profit, slow products, price effect, stock, lots | `facts.py`, `dm.py`, macros in `sql/store.py` |
| Replay of the helpers on train gold | `datamate facts-check`, `eval/factcheck.py` |
| Unit conversions written in README prose | `ingest/readme.py`, table `README.QuyDoi` |
| Lookup sheets for abbreviated names, staff nicknames, customer group on the lines | `canon.py` |
| Shops without line items: one row per order or ticket | `canon._order_level` |
| Query lint: ×100 on rates, `count(*)` on lines, walk-ins in customer groups, `avg()` of prices | `sql/agent.lint` (`text2sql`) |
| Pandas lint: a program that skips a helper or works at the wrong grain gets one rewrite, never a block | `agent/lint.py` (both branches) |
| Model places fact columns of a shop the rules cannot read; the table's shape must fit the fact | `agent/columns.py` |
| Table selection experiment: stage `dev120_oracle`, `--keep-tables assets/oracle_tables.json` | `text2sql` |
| Contract answer used when the repair still disagrees; "% đơn bị huỷ" now matches the contract | `pipeline.py`, `eval/semantic.py` |

**Measured.**

| check | result |
|---|---|
| `facts-check` on train | 398 exact, 20 unanswerable right, 0 wrong, 26 not parsed, of 444 |
| Macros vs helpers, 30 shops | no difference |
| `semantic-check`, `sql-check` | 0 misses; `cancel_rate` 139/139 |
| `fast-check` | 759/759, coverage 25.3% (public 7.4%) |
| `eval-checks` | 0 false alarms on 2930 gold answers |
| Tests | 279 on `codegen`, 312 on `text2sql` |
| Local model, 32 train questions wrong before, SQL arm | before 0 / 23 / 9, after 19 / 8 / 5 |
| Same 32 questions, pandas arm | before 7 / 12 / 13, after 22 / 4 / 6 |
| Fact columns, 6 shops with no README and headers `Cột N`, 30 facts | rules alone 0, model 5 (1 of them with a wrong total) |
| Same model call on 5 ordinary shops | 0 roles added |

**Known gaps.** With all headers renamed most facts stay off even with `agent/columns.py`, so those questions fall back to the model. A question with no date ("từ hồi X lên giá") needs the price table. Debt in shops with only a `Còn nợ` snapshot has no helper. The
full pipeline has not been run on all of train or on public yet.

**Table selection.** JEV is TypeSafe AI's hosted decision model, not JEPA; no open weights, so it cannot go in the offline image. Rule set by the team: if `dev120_oracle` beats
`dev120` by under 0.01 PA, drop table selection; if by much more, a small embedder of about 0.6B is enough.

**Kaggle runs pushed on 2026-11-07.** SQL v3 mounted a stale dataset and ran old code `4abdef9` (train 0.6857), so it does not count for G1. `datamate-sql-run` v4 runs `249b031` and was `RUNNING` at handoff; save it to `outputs/kaggle/result_sql_v4` and check `out/BUILD.txt` first. Pandas v3 scored train 0.8333, public 0.554; the `dm` fixes made after that are on `codegen` only (see its README "Status (2026-11-07, evening)"). Two accounts, work split evenly, one arm each; each run takes 4.5 to 5 hours.

| account | token in `.env` | arm | commit | kernel | dataset | stages |
|---|---|---|---|---|---|---|
| `hydrochii` | `KAGGLE_API_TOKEN` | SQL | `249b031` | `datamate-sql-run` v3 | `datamate-bundle-sql` | dev120, dev120_oracle, train, public |
| `hydrochiii` | `KAGGLE_API_TOKEN_2` | pandas | `28acb2a` | `datamate-codegen-run` v1 | `datamate-bundle-pandas` | dev120, train, public |

Output is available only once a kernel ends. Under zsh, run commands with several arguments in a variable from a bash script. For the second account, set the token for that command only and never print it:

```bash
uvx --with six kaggle kernels status hydrochii/datamate-sql-run
KAGGLE_API_TOKEN="$(grep '^KAGGLE_API_TOKEN_2=' .env | cut -d= -f2-)" uvx --with six kaggle kernels status hydrochiii/datamate-codegen-run
```

To download: `kaggle kernels output --file-pattern` filters only inside the first page of files, so it misses per-question logs. Page through
`list_kernel_session_output` instead (`ApiListKernelSessionOutputRequest` from `kagglesdk`, `page_size=200`, `user_name` set to the account, run with `uvx --with six --from kaggle python`) and fetch each file's `url`.
Save to `outputs/kaggle/result_sql_v3` and `outputs/kaggle/result_codegen_v3`. First check that `out/BUILD.txt` names the commit above, since the push does not prove the kernel mounted the new dataset version.

**Next:**
1. Score `dev120`, `dev120_oracle`, `train` and `public`, and compare with the 2026-11-06 runs (train SQL 0.687, pandas 0.669, public 0.356).
2. Apply the table selection rule above.
3. Leave the public submission to the user: label scorings are limited to about 3 or 4 before the deadline, 2 used so far.
4. Decide directions 2 and 10.

**Open items.** `orders` on `biz_109` still takes a wrong total in the renamed-column test. Debt in shops with only a `Còn nợ` snapshot and "3 tháng liền" are not handled. `dev_010` debt_top is not investigated. `fast-check` and `mutate-check` exist only in the `codegen` CLI; the fast branch itself runs on both. `ground_truth.json` stays on `origin/main` until the user starts the Docker image, and must not be merged into a working branch.

## Status (2026-11-06, night)

**Branches.** Shared code (ingest, eval, rules, fast branch) is the same on both. Nothing here is pushed to GitHub.

| branch | folder | head | holds |
|---|---|---|---|
| `codegen` | `zalo_ai/` | `6514434` | pandas arm and shared work |
| `text2sql` | `../zalo_ai_text2sql/` (worktree) | `1d23b0f` | SQL arm, shared work, SQL extras |

Everything is committed, at `INGEST_VERSION` 16 (`semantic-check` 766/824, 0 misses). The worktree has no `data/`; link it and
run with `PYTHONPATH=src`. Do not commit `assets/BUILD.txt` on `codegen`. `pipeline.py` and `cli.py` differ between branches.

**Kaggle runs.** All three use code `4abdef9` (hash `f0d7688`, ingest 12), older than the work below.

| kernel | run | use |
|---|---|---|
| `datamate-sql-run` v2 | 3000 train, SQL | G1 |
| `datamate-codegen-run` v2 | 3000 train, pandas | G1 |
| `datamate-public-sql` v1 (second Kaggle account) | 1000 public, SQL | test submission, sent by hand |

G1: download both train runs, then `datamate compare <pandas run> <sql run>`. Logs hold no token counts, so `compare` reports seconds and model calls.

**SQL on dev120, Qwen3.5-9B:** PA **0.650** (95 / 17 / 8). No missing-fence errors; misses are meaning errors (×100 on `cancel_rate`,
stock, debt). Logs: `outputs/kaggle/result_sql_dev120`.

**Built today.** None of it has run end to end on questions with Qwen yet.

| item | where | measured |
|---|---|---|
| Mutation set | `datamate mutate-check`, `eval/mutate.py` | table below |
| Role checks by value | `ingest/trust.py` | 44 shops: 0 false `suspect` |
| Column mapping by the model | `agent/columns.py`, saved as `<biz>.roles.json`; asked only when a needed role or the status is missing | 30 train shops, no mutation: 766/824, 551/551 roles |
| Totals found by value | `_block_totals` in `ingest/business.py` | table below |
| Rule gate | `rules.py` | Qwen: 8/8 changes caught, 2/2 rewordings kept (10 variants); train and public rulebooks need no model call |
| Fast branch | `fast.py`, `--fast` (off by default), `datamate fast-check` | train 703/703 at 23.4% coverage; public coverage **7.0%**, target 30% not met; added clauses 7030/7030 sent to the model; gates 703/703 |
| SQL extras (`text2sql` only) | `examples_sql.jsonl` read when present, LIMIT needs ORDER BY, store named by saved labels | `sql-check` 766/824, 0 misses (ingest 15; not rerun on 16) |

Contracts kept (no mutation: 766/824 on 30 shops, 192/200 on 8 family shops):

| mutation | rules, 30 shops | rules, 8 shops | Qwen, 8 shops | roles, Qwen |
|---|---|---|---|---|
| shuffle, formats, name spellings | 766 | | | |
| no README | 565 | 148 | **192** | 152/163 |
| new headers, README kept | 719 | 157 | **185** | 116/163 |
| new headers, no README | 0 | 0 | **185** | 100/163 |
| unknown status words | 44 | 6 | **192** | 163/163 |
| subtotal rows, unknown label | **766** (526 before the fix) | | | |

Contracts cover five simple metrics, so 100/163 roles matters more than 185/200 for product and unit questions. The rules and the fast-branch
words were written on these shops, so these numbers can be optimistic.

**Left out on purpose:** industry detection (READMEs rarely name the industry), deterministic unanswerable for `absent`, a generic transaction
table, leave-family-out for the fast branch, W4 (needs the G1 winner).

**Next:** G1 when both train runs finish; validate and submit the public file; rerun `sql-check` on ingest 16; run the new code on dev120 with Qwen;
open the PRs; W4 on the winner.

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
