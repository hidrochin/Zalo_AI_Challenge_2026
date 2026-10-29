# DataMate: Solution Proposal

**Constraints:** solo, under 2 weeks to the frozen Docker submission, prompting only (no fine-tuning).
**Dev compute:** Mac, free Kaggle GPUs, short cloud-GPU rentals.
**Architecture:** hybrid (deterministic ingestion, then LLM-written pandas over standard tables, then voting and abstention).
**Facts:** [CLAUDE.md](../CLAUDE.md) has the data and rules analysis; this document doesn't repeat it.

---

## 0. TL;DR

1. **Ingest each business once, without an LLM where possible.** Read every file and sheet, find the
   header rows, merge month sheets and split files, parse messy values, and map columns to standard
   roles using the README vocabulary. The output is a set of **canonical tables** (orders, lines,
   products, …) plus a **schema card** that tells the LLM what exists and what is missing.
2. **One ≤ 15B LLM writes a short pandas program per question.** The program calls a small
   helper library (`dm`) for time windows, valid orders, revenue, ranking, debt, stock and
   answer formatting. The prompt holds the split's rulebook, the helper docs, the schema card and
   3–5 retrieved, verified examples.
3. **Run it in a sandbox and vote.** Take one greedy sample plus a few seeded samples, execute
   them all, and normalize the answers the way the scorer does. Answer only when agreement and
   sanity checks clear a threshold tuned on train to maximize PA; otherwise abstain.
4. **Claude is used in development only, and only on train data.** It writes reference programs
   for all 3000 train questions against our helper API. Programs that reproduce the gold answer
   become the example bank. Programs that fail point to bugs in ingestion or the helpers.

Rough effort split: ingestion 40%, helpers and prompt 25%, abstention and calibration 15%,
Docker, timing and determinism 20%.

---

## 1. Where the points come from

`PA = coverage × (2 × precision_on_answered − 1)`. For example, answering 85% of questions at 85%
precision scores 0.595, and answering 70% at 93% precision scores 0.602. **Precision matters
more than coverage.** Answer only if P(correct) > 0.5.

Expected error sources, and the part of the system that addresses each:

| error source | example | addressed by |
|---|---|---|
| ingestion | header row missed, `55,000` read as 55, Excel serial date, status `đã lấy` not mapped, continuation rows not forward-filled | §2.1 and ingestion tests |
| rule semantics | order with a partial return left in revenue, prior-month comparison not trimmed to the same number of days, walk-in customers ranked | `dm` helpers that encode the defaults, plus the rulebook in the prompt |
| question understanding | wrong period, wrong product (`lavie 1.5l` written without diacritics), wrong answer type | deterministic entity and date hints, examples |
| formatting | `0.124` instead of `12.4`, string in place of a list, wrong rounding | `dm.answer_*` enforces the format |
| overconfidence | answering an unanswerable question, answering a low-agreement question | voting and calibrated abstention (§2.6) |

---

## 2. Architecture

```
            ┌─────────────── once per business (CPU, cached) ───────────────┐
/data/<biz> │ read → detect header/table → union sheets/files → parse values │
            │ → map columns to roles (README vocab, LLM fallback)             │
            │ → canonical tables + leftover tables + schema card + entity idx │
            └───────────────────────────────┬───────────────────────────────┘
                                            │
question ─► hints (dates, entities, type) ─►│
                                            ▼
   prompt = [system + output contract | rulebook | dm API doc]   ← cached for whole split
          + [schema card]                                        ← cached per business
          + [3–5 retrieved examples | question | as_of | hints]  ← per question
                                            │
                                vLLM (greedy + m seeded samples)
                                            │
                    sandbox exec (forked worker, data in memory, timeout)
                         │ error → 1 repair round with the traceback
                                            ▼
           normalize answers → vote → sanity checks → answer | unanswerable | abstain
                                            │
          append to /result/submission.jsonl + /result/logs/<id>.json (flush every line)
```

### 2.1 Ingestion (`datamate.ingest`)

Generic code only, with nothing keyed to a business ID (Rule 4). Steps:

1. **Read raw.** Use calamine for xlsx (every sheet), pandas for csv (sniff encoding and
   delimiter), and sqlite3 for every table. Keep all cells as strings at first.
2. **Detect tables.** For each sheet, score the first ~10 rows by fuzzy match to the README column
   names and by the share of non-numeric strings. Pick the header row, and keep or merge the group
   row when the header spans 2 rows. Drop title rows (`DANH SÁCH…`), filter rows (`Trạng thái
   đơn hàng: …`, `Kỳ báo cáo…`), blank rows and trailing total rows.
3. **Union.** Merge sheets and files with the same columns into one logical table: monthly sheets,
   `_1/_2`, `_2025/_2026`, `BanHang_T1_2025.xlsx`. Keep `_src_file`, `_src_sheet` and the year and
   month taken from the sheet name, which supplies the year for dates written without one.
4. **Parse values,** choosing the format per column by majority:
   - money: `55,000`, `55.000đ`, `55k`, `1,6256tr`, `45,000.0000`
   - dates: dd/mm/yyyy, Excel serials, `1 thg 1 2025`, datetimes, dates without a year
   - quantities with a unit attached: `3 chai` becomes quantity 3, unit `chai`
   - booleans

   **Cross-check** `qty × price − discount ≈ amount` to settle the separator for each column and to
   flag parse errors.
5. **Map columns to roles.** The README descriptions are a fixed vocabulary for core retail
   columns: 193 distinct phrases across 1210 column entries. For example, `mã đơn` covers `Bill`,
   `Ma don`, `Mã GD`, `Mã bill`, `Mã chứng từ`. Build a phrase → role dictionary from the train
   READMEs. If a description is unknown (43% of public-test phrases are new) or there is no
   README entry, send one batched LLM call per business with the header, description, sample
   values and the role list, including `other`. Cache the result.
6. **Normalize by meaning:**
   - **Status:** map the distinct values (usually under 20) to {completed, cancelled, returned,
     shipping}, with keyword rules first and the LLM second. Returns can also be inferred from
     negative-quantity lines, return tables, lot rows with returned quantity > 0, or a notes column
     when there is no status column.
   - **Order rows:** forward-fill order fields onto continuation rows, and drop identical
     duplicate rows within the same order.
   - **Base units:** `qty_base` comes from the conversion table (DanhSachQuyDoi / DanhMuc /
     don_vi_sp / README) or from a given converted-quantity column. A unit with no conversion row
     is the base unit with factor 1.
   - **Entities:** for products, customers, staff, suppliers and groups, build a code ↔ all
     spellings map and a diacritic-free index for fuzzy lookup. Customers without a code are
     merged by normalized name ("chỉ khác cách viết").
7. **Emit.** Write canonical parquet files to `/tmp/work/<biz>/`. Tables the canonical model
   doesn't cover (repair log, trade-ins, warranty, shift cash report) stay as **cleaned leftover
   tables** with their README descriptions, so new business types still have usable data.

**Canonical tables** (filled where the data allows):
`orders(order_id, order_date, order_ts, status, customer_id, customer_name, customer_group, staff, channel, pay_method, goods_total, order_discount, shipping, vat, order_total, paid, debt_end)`,
`lines(order_id, line_no, product_id, product_name, unit, qty, qty_base, unit_price, line_discount, line_amount, cost_on_line)`,
`products(product_id, name, aliases, group, base_unit, units{unit: factor}, cost, rx, ingredient, …)`,
`customers`, `payments(order_id, ts, amount, method)`, `returns`, `imports(+lines)`, `stock_snapshot`,
`stock_moves`, `lots`, `lot_sales`, `price_history`, `expenses`, `deliveries`, `debt_terms`.

**Schema card** (about 1–3k tokens):
- each table with its columns, roles, README descriptions and 2–3 sample values
- **coverage dates per table**
- **capability flags**, for example `has_line_items`, `has_customer_codes`, `has_cost_on_line`,
  `has_payments`, `has_stock`, `has_lots`, `has_expenses`, `has_order_time`, `has_staff`,
  `has_customer_group`, and `has_gender=False`
- leftover tables

The flags are what make "the same question is answerable for one shop and not another" decidable.

**Ingestion tests:** for every one of the 44 businesses, check that order dates are never NaT,
every line maps to a product, all 4 statuses are covered, revenue for a sample month comes out the
same two ways, and coverage matches the README's data end date. Run them on the Mac in CI style
after every change.

### 2.2 Helper library (`dm`)

The LLM writes code against `dm`, not against raw pandas alone. Every call clips to `ctx.as_of`
automatically, except that order status is taken as recorded on the order, as the rulebook says.

| area | functions (sketch) |
|---|---|
| time | `day(d)`, `month(y,m)`, `month_no_year(m)` (this year unless it is after today), `month_to_date()`, `prev_month_same_days()`, `week_this()`, `week_prev()`, `last_n_days(n)`, `quarter(y,q)`, `ytd()`, `tet(y)`, `pre_tet(y)`, `until(d)`, `count_weekday(period, wd)`, `shift_of(ts)` |
| sales | `valid_orders(period, **filters)` (not cancelled, not returned, by order date), `valid_lines(...)`, `revenue(period, by=None, **filters)`, `qty_sold(period, unit=None, by=None)`, `cancel_rate(period)`, `n_orders(...)`, `avg_order_value(...)`, `pct_change(a, b)` |
| ranking | `rank(df, value, k, code_col)`: descending, ties by code then name, returns all if fewer than k |
| customers / staff | `customer_key()` (by code, or by normalized name), walk-in exclusion built in |
| debt | `debt_at(d, by_customer=False)`, `debt_aging(d, buckets)`, `debt_new_and_repaid(period, customer)` (COD and e-commerce orders excluded, payments made on the purchase day count as paid at purchase) |
| stock / imports | `stock_at(d)` (from the stock card when present, otherwise the snapshot minus later movements), `stock_value()`, `import_total(period)`, `avg_import_price(...)`, `slow_movers(period)`, `nearest_expiry_lot(product)`, `expiring_within(n)` |
| profit | `gross_profit(period, by=None)` (uses the cost on each line), `net_profit(month)` |
| entities | `find_product(text)`, `find_customer(text)`, `find_group(text)`: fuzzy lookup with diacritics ignored, returns candidates |
| guards | `require('payments', …)`: if the capability is missing, raises `Unanswerable(reason)`; period helpers raise it when a period lies **entirely** outside the coverage of the table being queried |
| answer | `answer_number(x, kind='money'|'count'|'avg_money'|'decimal')`, `answer_percent(x)`, `answer_date`, `answer_month`, `answer_bool`, `answer_list`, `answer_table(cols, rows)`, `unanswerable(reason)`: rounding and format are enforced here |

Each helper's docstring cites the rulebook clause it implements, and has flags for deviating
(for example `exclude_returned=True`). The prompt tells the model the **split's rulebook wins**:
if a clause differs from a helper default, it should pass the flag or compute directly. That
covers a private rulebook that changes or adds rules.

The helpers cover the frequent intents in train: revenue by period, % change, top-k by base
quantity, units sold of a base unit, group and customer-group shares, cancel rate, best day,
3-month monotonic growth, top debtors as a table, debt aging, new debt vs repaid, end-of-day
stock, stock value, slow movers, price-increase effect, gross and net profit, expiry, Tết vs
pre-Tết, and morning vs afternoon shift.

### 2.3 Prompt

The order of the prompt is designed around **vLLM's automatic prefix caching**:

1. **Static for the whole split:** the system prompt and output contract ("return Python that
   ends in exactly one `dm.answer_*` / `dm.unanswerable` call"), the full split `rulebook.md`
   (roughly 8–10k tokens, never trimmed because the rules change per split) and the `dm` API
   docs.
2. **Per business:** the schema card. All questions for one business run together, so this is
   prefilled once.
3. **Per question:**
   - 3–5 examples (question, verified code, answer)
   - the question and `as_of`
   - deterministic **hints**: explicit dates normalized (`th7/26` → 2026-07, `quý 2/2026`,
     `6-2025`), entity candidates from the fuzzy index (`lavie 1.5l` → `SP0012 "Nước suối Lavie
     1.5L"`), and a guess at the answer type from wording cues (`%`, `ngày nào`, `có … không`,
     `top k`, `kèm số tiền` → table)

The output is a short `# plan` comment followed by code. Whether to use "thinking" mode is decided
in the bake-off.

**Example retrieval:** char-3-gram TF-IDF / BM25 over diacritic-free text with digits masked. It
has zero parameters, so the 15B budget is untouched. Results are filtered for diversity and
preferring examples whose required capabilities the current business has. During evaluation,
examples from the same business are excluded.

### 2.4 Execution

Each business gets a **fork-server worker** that holds its canonical tables in memory. Each
program runs in a forked child with copy-on-write data, which takes milliseconds, under a 20 s
timeout, a memory cap, no network and a read-only `/data`. stdout, stderr and the answer object go
back through a pipe. On an exception, the traceback goes back to the LLM for **one** repair
attempt.

### 2.5 Runtime orchestration (`predict.sh`)

- At t=0, start the two vLLM engines (one per GPU, data-parallel, since requests are independent)
  and run CPU ingestion of all businesses **in parallel** with model loading. There is 512 GB of
  RAM and many cores.
- Process questions business by business, sending requests asynchronously so both GPUs stay full.
- After each question, **append** it to `submission.jsonl` and flush, and write the log JSON (id,
  answer, elapsed, prompt hash, code, stdout, votes, decision).
- **Time guard:** track throughput against a 5 h 30 m budget. If behind, lower the sample count
  m, then skip repairs. Unfinished questions are left missing, which scores as abstain.
- Rough throughput, to be measured on D11: about 3k new prefill tokens and 0.4–0.6k decoded
  tokens per sample. With ~4 samples, 3000 questions is about 6–8M decoded tokens. On 2× 5090 with
  a 9–14B FP8/AWQ model that should take **about 1–2 h**, which leaves room for adaptive extra
  samples. The private-test size is unknown, so the time guard is mandatory.

### 2.6 Voting, unanswerable and abstain

- **Adaptive sampling:** run greedy plus 2 seeded samples (T≈0.6). If all 3 agree after
  normalization, stop. Otherwise add samples up to 6–8. Seeds are fixed for reproducibility.
- **Features:**
  - vote share of the top answer and number of distinct answers
  - execution-error count, and whether repair was needed
  - whether the answer type matches the hint type
  - sanity checks: money ≥ 0, percent finite, list length = k (or all candidates), date inside
    the asked period, string present in the entity index
  - the precision of that intent family measured on train
- **Decision:** a tiny logistic model (or hand thresholds) on those features gives P(correct).
  Answer if it exceeds τ, with τ tuned on train under leave-business-out to **maximize PA**,
  not accuracy.
- **Unanswerable policy:** only 2.3% of references are unanswerable, and a wrong
  "unanswerable" costs −1. Emit it only when a **deterministic guard** fired (missing
  capability, period entirely outside coverage) or a strong majority of samples choose
  `dm.unanswerable` (for subjective or external-knowledge questions). Otherwise abstain.

---

## 3. Model

**Budget:** ≤ 15B parameters in total, so there is one LLM and no embedder or reranker
(retrieval is TF-IDF). Count parameters from the checkpoint's safetensors index, including any
vision tower.

| candidate | params | released / licence | notes |
|---|---|---|---|
| Qwen3.5-9B | 9B | 2026-03-02, Apache-2.0 | newest small Qwen, 262K context, includes vision. Its newer architecture **may lack T4 kernels** on Kaggle, so verify |
| Qwen3-14B | 14.8B | 2025-04, Apache-2.0 | strong Vietnamese, thinking and non-thinking modes, mature vLLM support. Uses almost the whole budget |
| Qwen2.5-Coder-14B-Instruct | 14.7B | 2024-11, Apache-2.0 | strongest at pure pandas, weaker at Vietnamese and reasoning |

The 2026 Qwen coder models I found (Qwen3.6/3.8-27B, Qwen3-Coder-Next 80B MoE) are all over the
cap. **On D4, check Hugging Face for any newer ≤ 15B release dated before 2026-10-01** before
running the bake-off.

**Bake-off (D4–D5):** run the full pipeline on `train-dev-300` with the verified examples. Test
each model with thinking on and off, in FP8 or AWQ-int4. Compare PA after calibration, precision
and coverage, and tokens per question. Pick one and freeze it.

---

## 4. Example bank from Claude (dev only, train only)

1. For each of the 3000 train questions, give Claude the **same prompt our local model will get**
   (rulebook, `dm` docs, schema card, question) and execute the returned program on our
   canonical data.
2. Score it against `answers.jsonl` with the local scorer, and keep the **verified** programs.
   Include the 70 unanswerable questions, which teach `dm.unanswerable` and `require`.
3. **Every failure** points to one of four things: an ingestion bug, a missing or wrong helper, a
   rulebook subtlety, or a bad gold reading. Triage failures per business. The per-business pass
   rate is the main **ingestion health metric**.
4. Store examples as `{question, as_of, intent_tag, capabilities_used, code, answer}`.

**Hard rules:** never send public or private test questions or data to any external API (Rule 6
forbids collecting answers to test questions by any means). Examples come from train only. Ask the
organizers to confirm that API-generated synthetic data is fine.

---

## 5. Evaluation

- **Scorer** (`datamate.eval.scorer`): implements the evaluation table exactly.
  - Strings: NFC, lowercase, non-alphanumeric characters → space, collapse whitespace, accept any
    alias.
  - Percents: round to 2 dp.
  - Lists: compared by position.
  - Tables: columns matched by name, else by position; rows compared as a set.
  - Output: PA, coverage, precision, broken down by type, intent family and business.
- **Splits:**
  - `train-dev-300`, stratified by answer type and data family, for quick runs on Kaggle
  - full train with **leave-business-out** retrieval, for calibration
  - a **leave-family-out** stress test (for example, no pharmacy examples when testing pharmacy),
    to estimate behavior on unseen business types
- **Public leaderboard:** submit at milestones only (D6 baseline, D8 calibrated, D11 final
  candidate). Use the public data to check *robustness*: crashes, ingestion warnings, coverage
  anomalies on new formats. **Never** hand-compute public answers.

---

## 6. Compute plan

| where | used for |
|---|---|
| Mac | ingestion, `dm`, scorer, tests, sandbox, the Claude example bank (API, CPU only). Optional smoke tests with an MLX 4-bit 9B model if RAM allows |
| Kaggle 2× T4 (16 GB each, about 30 GPU-h/week) | vLLM runs on `train-dev-300`. T4 has no bf16, FP8 or FlashAttention-2, so use fp16 or AWQ, and fall back to Qwen3-14B-AWQ if Qwen3.5 doesn't run |
| rented 5090 (one or two short sessions) | model bake-off, full-train calibration run, and the **final Docker dry run**: timing, determinism (two runs compared), `--network none` |

---

## 7. Day-by-day plan (D1 = Fri 2026-10-30; shift if the deadline differs)

| day | deliverable | done when |
|---|---|---|
| D1 | repo skeleton, scorer, raw reader + table detection + union for all 44 businesses | every sheet resolves to a header and a logical table; inventory report printed |
| D2 | value parsers, README role dictionary, canonical builder for the KiotViet-like, Sapo-like, sổ chi tiết, CSV-relational and sqlite families | those families pass the ingestion tests |
| D3 | remaining families (hand-kept sổ, flat CSV, order-only, mixed sources), status/returns/units/dedupe, schema card, entity index | all 30 train businesses pass the tests |
| D4 | `dm` helpers + sandbox; check Hugging Face for new models; start the Claude example bank | Claude programs run end to end on train |
| D5 | triage the example bank and fix ingestion/helpers (loop); bake-off on a rented GPU | train oracle pass rate ≥ 85%; model chosen |
| D6 | local pipeline: vLLM, prefix-cached prompt, retrieval, hints, exec+repair, greedy only | **first public submission** (baseline) |
| D7 | voting, adaptive sampling, error analysis by intent family | train-dev PA tracked per family |
| D8 | abstention features, τ calibration (leave-business-out), unanswerable policy | **second public submission** |
| D9 | generalization hardening: LLM role-mapping fallback, leftover tables, new-industry rules via the rulebook, robustness pass over public formats | 0 crashes and 0 empty canonical tables on public |
| D10 | Docker: CUDA ≥ 12.8 base, vLLM, weights baked in, `predict.sh`, logs, time guard, README listing the models | runs offline locally on a CPU smoke subset |
| D11 | dry run on rented 2× 5090: timing on public (1000 q) plus a train slice, determinism diff | PA diff between two runs ≪ 0.03; timing extrapolates under 4 h for 3000 q |
| D12 | buffer, freeze, final submission | image pushed |

**If behind schedule,** fall back in this order: (1) skip canonical tables for rare families and
let the LLM use the cleaned leftover tables directly; (2) drop the repair round; (3) raise τ
(precision-first).

---

## 8. Risks

| risk | mitigation |
|---|---|
| the private rulebook changes or adds rules | the full split rulebook is always in the prompt; helper flags; "the rulebook wins" instruction; no train-only rules hard-coded without flags |
| unseen business types and formats (43% of public README phrases are new) | generic table detection, LLM role-mapping fallback, leftover tables in the schema card, examples that teach working from raw tables |
| numeric or date parse ambiguity | per-column majority vote, `qty×price≈amount` cross-check, README hints, tests |
| Kaggle T4 can't run the chosen model | use AWQ Qwen3-14B on T4; do the bake-off on a rented 5090 |
| vLLM nondeterminism | greedy decoding for the main sample, fixed seeds, stable sorting everywhere, compare two runs on D11, keep full logs |
| time overrun on a large private set | incremental writes, time guard, adaptive m, ingestion overlapped with model loading |
| unknown Docker base image | assume CUDA 12.8 + PyTorch + vLLM for Blackwell; switch as soon as the guideline arrives |
| solo bandwidth | strict day gates, fallbacks above, no fine-tuning |

---

## 9. Compliance checklist

- [ ] One model, ≤ 15B total parameters (counted from the checkpoint), released before 2026-10-01, permissive licence, declared in the README
- [ ] No network at inference; weights inside the image; run tested with `--network none`
- [ ] No logic keyed on question or business IDs; examples retrieved by text similarity only
- [ ] `/data` treated as read-only; all scratch files in `/tmp/work`
- [ ] Public and private test data never used for training or examples, and never sent to external APIs
- [ ] `/result/submission.jsonl` written incrementally; `/result/logs/` has id, answer, elapsed time, code and output
- [ ] Deterministic within 0.03 PA across two runs

---

## 10. To confirm

- Docker guideline (base image, CUDA/vLLM versions) and the exact solution deadline
- Expected private-test size (affects m and τ), and the daily submission limit for round 1
- Whether organizers allow API-generated synthetic data during development

---

## Appendix: repo layout

```
src/datamate/
  ingest/   readers.py  tables.py  parsers.py  roles.py  canonical.py  schema_card.py  entities.py
  dm/       context.py  time.py  sales.py  rank.py  debt.py  stock.py  profit.py  answer.py  guards.py
  agent/    prompt.py  hints.py  retrieve.py  llm.py  sandbox.py  vote.py  pipeline.py
  eval/     scorer.py  run_eval.py  calibrate.py
scripts/    build_examples_claude.py  bakeoff.py  ingest_report.py
assets/     examples.jsonl  role_vocab.json
docker/     Dockerfile  predict.sh  README.md
tests/      test_parsers.py  test_ingest_all.py  test_dm_time.py  test_scorer.py
```
