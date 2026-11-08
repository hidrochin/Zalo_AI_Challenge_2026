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

## Status (2026-11-08): start here

The same section is in the README of all three branches. Each branch has its own worktree. Nothing is pushed to GitHub.

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
