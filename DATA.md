# DataMate: problem diagnosis and data analysis

Status: analysis only. This document doesn't choose an architecture or a roadmap.
Date: 2026-11-02. Evidence scripts and their raw output are in [debug/](debug/):
[q_stats.py](debug/q_stats.py) → `q_stats.txt`, [data_stats.py](debug/data_stats.py) → `data_stats.txt`,
[sub_stats.py](debug/sub_stats.py) → `sub_stats.txt`.

Findings are tagged by how well they are supported:
- **[E]** evidence: measured on the data, or stated in the official description or rulebook
- **[H]** hypothesis: plausible, but not verified
- **[A]** assumption: something we currently rely on that could be wrong

---

## 1. Problem overview and formulation

### 1.1 What the task says
Given one small business's raw files (CSV / XLSX / SQLite), a README, a split-level rulebook, a lunar
calendar, a Vietnamese question and an `as_of_date`, return one typed answer, or `unanswerable`, or
`abstain`. **[E]** (`description/description.txt`, `data.txt`)

### 1.2 What the task actually is
This is not a text-to-SQL or table-QA problem in the usual sense. Breaking it down, it is three coupled problems:

1. **Schema recovery.** Map messy, inconsistently exported files onto business concepts: order, line,
   product, base unit, customer, status, payment, debt, stock and lot. Each business does this differently
   and the README quality varies, so this is closer to *data wrangling under uncertainty* than to parsing.
   **[E]** (section 2.3)
2. **Specification resolution.** Turn an informal, typo-heavy Vietnamese question into an exact computation.
   The question alone almost never pins down the computation. The **rulebook supplies the defaults**: which
   orders count, how revenue is defined, what "tuần trước" means, tie-breaking, rounding and units. The answer
   is the value under *the organizers' reading* of the question, not a reasonable reading. **[E]**
3. **Decision under a penalized metric.** Decide answer / unanswerable / abstain. A wrong answer costs as
   much as a correct one gains. **[E]**

The capability being tested is **"compute the organizer's reference value exactly"**, not "understand the
question". A semantically sensible answer that differs from the reference by 1 đồng, or uses another valid
definition of revenue, scores −1.

### 1.3 Inputs, outputs and constraints

| aspect | detail | tag |
|---|---|---|
| Input per question | `{id, business_id, question, as_of_date}` plus the business folder, the split's `rulebook.md` and `calendar.csv` | E |
| Output | one JSON line per id. Types: number, percent, string, date, boolean, list, table, unanswerable, abstain | E |
| Matching | type must equal the reference type; values match exactly (2 dp for percent; strings normalized, diacritics required, aliases and item codes accepted; lists by position; tables as a set of rows) | E |
| Models | open weights released before 2026-10-01, **≤ 15B params in total across every model used** | E |
| Runtime | offline, 2× RTX 5090, **6 h wall clock for the whole private test including model load**; only answers written before a cutoff count | E |
| Reproducibility | the organizers re-run the image; \|ΔPA\| ≤ 0.03 or risk disqualification | E |
| Prohibited | training on test data, logic keyed on IDs, hard-coded answers, human in the loop | E |

### 1.4 Metric
`PA = (correct − wrong) / N`, with abstain = 0. **[E]**
- With calibrated confidence p, answering pays off iff p > 0.5. The expected gain per answered question is 2p − 1. **[E, arithmetic]**
- PA rewards *precision at a coverage level*, not accuracy. At 80% precision, every answered question is worth +0.6.
  At 60% it is worth +0.2. At 40% it costs 0.2.
- Statistical resolution: the per-question score has standard deviation ≈ 0.8–0.9, so the standard error of PA is
  ≈ **0.07 on dev120** and ≈ **0.025–0.03 on 1000 questions**. Two dev120 runs that differ by less than ~0.1 cannot
  be told apart. The 0.03 reproducibility tolerance is about one standard error of a 1000-question set, so it is
  tight. **[E, arithmetic]**

---

## 2. Dataset characteristics and findings

### 2.1 Splits

| split | businesses | questions | per business | answers |
|---|---|---|---|---|
| train | 30 (`biz_101`–`130`) | 3000 | 40 for 7 flat-file businesses, ~118 for the rest | yes |
| public_test | 14 (`dev_001`–`014`) | 1000 | 21–129 | no |
| private_test | ? | ? | ? | no; contains **business types unseen** in train and public |

**[E]** Train businesses with a single flat file (biz_101/107/110/113/114/125/126) get only 40 questions each. This
suggests the organizers generate questions **per capability present in the schema**. Richer schemas get more
question templates. **[H]**

### 2.2 Answer types (train, n = 3000) **[E]**

| number | percent | list | date | string | unanswerable | boolean | table |
|---|---|---|---|---|---|---|---|
| 1884 (62.8%) | 578 (19.3%) | 193 | 145 | 88 | 70 (2.3%) | 23 | 19 |

- Every number answer is an integer. 49% are ≥ 10⁶ (money in VND) and 21.5% are < 100 (counts). Only 2 are 0
  and only 2 are negative. **[E]**
- Percent answers: 23.5% negative (period-over-period change), 2 are zero, 8 have |x| > 100. **[E]**
- List lengths are only 2, 3 or 5 (one list has 11). Of the elements, 426 are strings (top-k names) and 201
  are ints (multi-period values, aging buckets). **[E]**
- Dates: 129 `YYYY-MM-DD` and 16 `YYYY-MM`. **[E]**
- 167 answers have aliases (spelling variants plus the item code). **[E]**

**Implication.** A zero answer is almost never right: the prior is 2/1884 for numbers and 2/578 for percents. A 0
is much more likely to come from an empty filter, a wrong join or a missing column than from a true zero.

### 2.3 Data: structure, size and quality **[E]** (`debug/data_stats.txt`)

- **Size:** 6k to 306k cleaned rows per business. Each business spans **12–20 months**, starting between
  2025-01 and 2025-07 and ending in 2026-05 to 2026-09.
- **Tables per business:** from 1 (a flat sales book) up to 17 (`pos.sqlite`).
- **Recurring export "families":** the same underlying schema appears under different layouts and naming
  (CLAUDE.md §3.3). Public businesses relative to train families:

| public biz | family | seen in train? |
|---|---|---|
| dev_001 | "Sổ chi tiết bán hàng" | yes (biz_112/115/116/118/130) |
| dev_002, dev_005 | Sapo-like `DonHang_ChiTiet` (+ pharmacy lots) | yes (biz_102/103/121/122) |
| dev_003 | mixed software export + hand notebook + shift report `BaoCaoCa` | partly (biz_124); shift report is new |
| dev_004 | hand-kept `SoBanHang/DanhMuc/TonKho/SoNhapHang/SoGiaoHang/SoHanNo` | yes (biz_117: nearly identical table set) |
| dev_006 | `pos.sqlite`, 17 tables | yes (biz_123: identical table names) |
| dev_007, dev_008 | "3 danh sách: chi tiết / nhập hàng / danh mục" | **new** |
| dev_009 | order-only `danh_sach_hoa_don.csv` | yes (biz_110) |
| dev_010 | CSV relational | yes (biz_111/127/129) |
| dev_011, dev_014 | phone shop (`Ghi tắt`, trade-in, installments, warranty) | **new industry** |
| dev_012 | motorbike repair (per-ticket log, mechanics, commission) | **new industry, no line items** |
| dev_013 | mother & baby (vouchers, points, e-commerce, 2-row headers) | **new industry** |

  About 9 of the 14 public businesses come from train families. The private test is announced to contain unseen
  types, so its share of unfamiliar structure is probably higher than in public. **[H]**

- **Capability coverage varies sharply.** Inventory, imports, lots, customers, debt, staff, returns, price history
  and unit conversion each exist for only a subset of businesses. Examples:
  - biz_113 has none of them.
  - biz_124 has all of them.
  - biz_110 and dev_009 have no line items at all.
  - dev_012's "sales" are repair tickets, with money per ticket and free-text work descriptions.

  **[E]** (`caps=` column in `data_stats.txt`)
- **Deliberate messiness**, catalogued in CLAUDE.md §3.4 and reconfirmed in the ingest cards:
  - mixed date formats in one column
  - money with suffixes (`55k`, `1,6256tr`)
  - quantities with units in the cell
  - forward-fill semantics for continuation rows
  - status only in notes
  - duplicate rows within an order
  - monthly sheets and split files
  - free-text name variants: in dev_012 one mechanic appears as `Đạt / a Đạt / A.Đạt`, and the `Xe` column
    contains 4155 distinct values for plate plus model

  **[E]**
- **README quality:** 0.9k–10k characters. Some READMEs are a full data dictionary, others a few lines (biz_110:
  911 chars, dev_009: 923). **[E]**
- **Records after the as-of date exist in the files.** 75–79% of questions have `as_of_date` = data end, but the
  p90 lag is about 240–260 days and the maximum about 550 days. A pipeline that forgets to clip to `as_of` is
  right for most questions, and so hides the bug on any small dev set. **[E]**

### 2.4 Questions **[E]** (`debug/q_stats.txt`)

| | train | public |
|---|---|---|
| no diacritics | 25.8% | 26.2% |
| median length | 59 chars | 55 chars |
| revenue-like intents (crude regex) | 44% | 42% |
| customer intents | 6.0% | **11.2%** |
| staff / shift / mechanic | 3.2% | **5.3%** |
| profit | 0.4% | **2.7%** |
| cancel / return | 8.5% | 4.8% |
| stock | 2.4% | 0.5% |

- **Distribution shift in intents.** Public has roughly 2× the customer questions, more staff questions and ~7× the
  profit questions. It has fewer cancel and stock questions. The intent buckets come from a crude keyword regex,
  so the exact numbers are approximate, but the direction is clear. **[E, method-limited]**
- **Low surface overlap with train.** After lowercasing, stripping diacritics and masking digits, only **4.2%** of
  public questions match a train skeleton verbatim. The median token-Jaccard to the nearest train skeleton is
  **0.46**; 54% are below 0.5 and 16% below 0.3. Intents repeat, but the phrasing does not.
  - Dialect appears in public: `ni`, `chừ`, `dề`, `mô`.
  - So do teen-code respellings: `kủa`, `zậy`, `jùm`, `bnhiu rooif`, `o đờ` (order).

  Example retrieval by lexical similarity will often return the wrong template. **[E]**
- **Same intent, different answer type.** "bn đơn bị huỷ" means a *number* (count). "tỉ lệ huỷ" means a *percent*.
  In dev120_v4, one question about cancelled orders got a percent where the gold answer was the count 65. Answer
  type is part of what the question specifies, and a wrong type alone makes the answer wrong. **[E]**

### 2.5 Unanswerable questions (train, 70 = 2.3%) **[E, manual grouping of the full list in `q_stats.txt`]**

| cause | ≈ count | examples |
|---|---|---|
| Field not recorded | ~45 | gender, street address, cancel/return *reason*, sales channel / payment method, no inventory file, no import file, no debt file, no customer column, no product group, no base-unit split |
| Period entirely outside the data range | ~16 | "tháng 2/2025" when the data starts 2025-04; "% vs the previous month" when the previous month is out of range |
| Subjective | ~8 | "lành nghề nhất", "khách quý mến nhất", "tận tâm", "lễ phép" |
| External knowledge | 1 | drug dosage |

- Unanswerables are concentrated in a few businesses (biz_103 and biz_112 have 12 each; 18 businesses have none).
  Whether a question is answerable depends on **schema plus date range**, not on its wording. The same template is
  answerable for one business and unanswerable for another. **[E]**
- **Partial overlap is answerable.** "Feb–Apr 2025" with data starting in Feb is answerable. In dev120_v4 the model
  declared a partial-overlap question unanswerable and lost a point. **[E]**
- These are the train ratios. The public test's unanswerable rate is unknown. **[A]** Its intent mix (more customer
  and staff questions, new industries) suggests the rate may be higher. **[H]**

### 2.6 Rulebook **[E]**
- The public rulebook (23.8k chars) is not the train one (18.5k).
  - It adds sections: Mẹ và bé, Sửa xe, Cửa hàng điện thoại, and more superlative definitions.
  - It removes some train rules.
- Industry rules are **highly specific and counter-intuitive**. A model's common sense won't produce them; they
  have to be read and applied. Examples:
  - Repair shop: average money per ticket is divided by *paid* tickets only, but the ticket count includes
    warranty tickets.
  - Repair shop: `Nhớt` alone means engine oil.
  - Repair shop: "sửa điện" excludes bulbs and batteries.
  - Phone shop: brand = first word of the name, with `iPhone` → Apple.
  - Phone shop: the model line is the part of the name before `…GB`, with `4G` not being a capacity.
  - Phone shop: installments count as debt, not as revenue.
  - Mother & baby: a diaper-size share uses only items whose Size column is filled in, regardless of the name.
- The rulebook states that unanswerable ≠ 0 and that partial-overlap periods are computed on the available part.
- **The private rulebook will contain rules that nobody has seen during development.** Any rule semantics learned
  only from train or public data cover just the rules that existed in those splits. **[E]** (description) / **[H]**
  (extent)

---

## 3. Core problems and technical challenges

Ordered by estimated impact on PA.

### C1. Exactness of aggregation semantics (main challenge)
About 82% of answers are number or percent **[E]**, and they match only if every default is right. That means:
- which orders count (cancelled, returned, any-line-returned, "đang giao")
- which amount (line after line discount, no order voucher, no VAT, no shipping)
- date assignment (order date)
- unit (base unit through the conversion tables)
- deduplication
- walk-in exclusion
- `as_of` clipping
- rounding (half-up to đồng, 2 dp)

One wrong default makes the answer wrong, and most of the logic is shared across intents. Evidence from dev120_v4's
27 wrong answers **[E]**:
- **Average order value off by a factor ~2** (biz_115, biz_112): wrong denominator, either lines vs orders or the
  counted-orders set.
- **"đơn có doanh thu ≥ X" answered 22381** where gold is 1215: lines counted instead of orders.
- **Gross profit ×3** (biz_109): revenue vs margin, or the cost source.
- **Cancel rate 44.94%** where gold is 3.16%: status mapping.
- **Top-k lists** with the right items in the wrong order or one item off: base unit, tie-break or period.
- **Debt tables**: empty rows, wrong column names, wrong debt definition.
- **Off-by-small counts** (60 vs 63 orders, 2953 vs 2965 cups): duplicates, `as_of` cut or status edge cases.

These aren't language-understanding failures. They are **definition and data-model failures**, and they repeat
across businesses.

### C2. Schema recovery on unseen layouts
Every computation depends on the right column getting the right role. On unseen families (dev_007/008, phone,
repair, mother & baby) the README phrases and headers are new. In dev_012 there is no line table; "what was done"
is free text and the mechanic names are nicknames. When a role is mis-assigned, the code runs and returns a
**plausible but wrong number**, which is the worst outcome under PA. **[E]** for dev_012 structure; **[H]** for
how often this happens.

### C3. Calibrated abstention and the unanswerable decision
Two different decisions are mixed together here:
- **unanswerable**: a claim about the data, which is +1 if right and −1 if wrong
- **abstain**: a claim about our own reliability, worth 0

With precision near 50% the system scores nothing, whatever its raw accuracy. Calibration has to hold on
**unseen** businesses, where precision is lowest, and that is exactly where train-based calibration is weakest.
**[E]**: the measured gap between dev120 and public, in §4.1.

### C4. Question understanding under noise
Typos, missing diacritics, dialect, teen-code, compressed dates (`5/25`, `th 7/26`, `6-2025`, `T11/2025`) and
multi-part questions are all present. Two examples:
- `5/25` could mean May 2025 or 25 May. The Kaggle run read it as `2026-05-25`, a day, and declared that
  out-of-range.
- Entity names must be matched to exact catalogue names, with diacritics.

This is real, but the evidence suggests it is **secondary** to C1–C3. The v3 wrong-answer triage found no format
problems and few misreadings; most failures were semantic. **[E]** (README Status)

### C5. Throughput, determinism and the parameter budget (systems challenge)
- Six hours for an unknown N. At 3000 questions that is ~7 s/question including load.
- The ≤ 15B total-parameter budget rules out an LLM + embedder + reranker stack of large models.
- Determinism has to survive a re-run on different hardware: Blackwell vs the T4/Mac used during development.
**[E]** constraints; §4.2 has the measured evidence.

Secondary issues: answer formatting (already handled), name normalization and aliases, lunar calendar windows,
week boundaries.

---

## 4. Potential bottlenecks and risks

### 4.1 What the first public submission actually shows **[E]** (`debug/sub_stats.txt`)
Score −0.110. Of the 1000 lines, 456 are abstain and 544 are answers.

1. **The LLM server stopped answering partway through dev_008, and the pipeline kept going.** Businesses are
   processed in sorted order. dev_009 through dev_014 (**340 questions**) are 100% abstain, and dev_008 is 40%
   answered. The Kaggle stdout shows the run *finished*: 1000/1000 in about 48 min, with the last ~290 questions at
   about 7 questions/s, which is too fast for any LLM call. Its `wrapt` messages appear at questions 710 and 731,
   exactly where dev_009 and dev_010 start. In `solve_question`, an exception from `llm.generate` ends the sample,
   so the vote abstains silently. **[E]** Why vLLM stopped (an engine crash, or the host OOM killer acting on a
   runaway generated program) is unconfirmed. The answer is in `out/vllm_Qwen3.5-9B.log` of that kernel version.
   **[H]**
   - The 340 skipped questions include all 4 new-industry businesses (dev_011–014) and dev_012, which has no line
     items. So the −0.11 says **nothing yet about unseen industries**.
2. **Precision on the attempted part was below 50%.** With 544 answers and PA = −0.11: ≈ 217 correct, ≈ 327 wrong,
   precision ≈ 40%. On the 710 attempted questions, the ≈ 217 correct and ≈ 327 wrong answers net to −0.15. **[E, arithmetic]**
3. **Self-inflicted unanswerables.** 49 unanswerable answers were given. **43** of them have the reason
   `"sales_lines() is not available for this shop; use T directly"`. The model turned a prompt note about a helper
   into a refusal for questions like "Dthu th3/2025". Almost all 43 are likely wrong, a cost of up to about
   −0.04 to −0.08 PA. **[E]** reasons / **[H]** correctness.
4. **Environment divergence.** For **dev_006** the Kaggle run reported `sales_lines()` as unavailable (14
   refusals). The local ingest of dev_006 *does* build `sales_lines` (`data_stats.txt`). The code, data or cache
   inside the Kaggle bundle differs from local. This is a **reproducibility hazard** of the same kind that the
   organizers' re-run would hit. **[E]** mismatch / **[H]** cause.
5. **29 zero answers**, despite a 0.1% prior for zeros. Several are train-style *unanswerable* templates that were
   answered with 0 instead: "khách nữ mua bao nhiêu" (×3), "khách ở đường Mai Chí Thọ", "đơn trả bằng chuyển
   khoản". **[E]** questions / **[H]** that these are unanswerable for those businesses.
6. **The dev120 → public precision drop is real even without these artifacts.** If all 43 helper-refusals and
   ~29 zeros were wrong, the other ≈ 470 answers would net about −0.04, which is still below 50% precision. On
   dev120_v4 the number and percent precision was ~80%. **[E, arithmetic on assumptions in 3 and 5]**

### 4.2 Evaluation and validation risks
- **dev120 isn't representative.** It samples train businesses whose families, rulebook and question phrasing match
  the examples and the tuning. Leave-business-out within train doesn't simulate an unseen *family* or *industry*:
  biz_123 ↔ dev_006 and biz_117 ↔ dev_004 are near clones. **[E]** (§2.3)
- **dev120 is too small to rank changes.** The standard error is about 0.07 (§1.4). **[E]**
- **The public leaderboard is a weak proxy for private.** The private test has a different rulebook, unseen
  industries and an unknown size. Tuning to the public score risks overfitting to 14 businesses, and repeated
  submissions leak test-set signal into design decisions. Prompting decisions informed by public *scores* are
  allowed, but they bias toward public. **[H]**
- **Hidden-bug masking.** 75–79% of questions have `as_of` = data end, so missing `as_of` clipping goes unnoticed on
  most questions. The same holds for other rare defaults: within-order duplicates, returns dated after the period,
  ties excluded at the k boundary. **[E]**
- **Gold answers are an organizer computation.** They come from a reference implementation of the rulebook, not
  from human judgment. Where the rulebook is ambiguous, only matching the reference implementation scores. The
  70+167 aliased and unanswerable cases show some of its edge-case choices; the rest has to be inferred. **[H]**

### 4.3 Compute and scalability risks
- **Throughput is unmeasured on the target hardware.** Measured so far:
  - Mac M2 Pro, Qwen 9B Q4, 1 sample: ~16–30 s/question.
  - Kaggle 2× T4, concurrency 16: the first ~710 questions took ~47 min, about 4 s/question in wall time.

  The prompt is 13–16k tokens per question. Prefix caching helps only if the prompt prefix is shared per business.
  At the T4 rate, 3000 private questions would take about 3.3 h with one sample. The 5090s should be faster, but
  sampling 3× or adding repair rounds multiplies the cost. **[E]** measurements / **[H]** extrapolation.
- **A time-limit cutoff costs abstains, not errors.** Questions that are never written score 0. Processing order
  decides *which* questions are lost. In the public run, a mid-run failure combined with sorted business order meant
  that all the new-industry businesses were lost. **[E]**
- **Ingestion cost.** openpyxl over the train XLSX took over 10 min; calamine plus a cache helps. Private data has
  to be ingested cold inside the 6 h, with no cache. **[E]**

### 4.4 Generalization risks
- **New rules in the private rulebook.** Any deterministic helper encodes *train/public* rules. When the private
  rulebook changes or adds a rule, a helper that silently applies the old rule is confidently wrong. **[H]**
- **New industries without "orders + lines".** dev_012 (repair tickets) and biz_110 / dev_009 (order-only) already
  break the "sales_lines" abstraction. **[E]** The private industries may have no order/line structure at all, for
  example a service business or an F&B shop with table service and shifts. **[H]**
- **Small-model reasoning ceiling.** A ~9–14B model must read a 20k-char rulebook and apply the one relevant clause.
  How reliably it does so for rules it has never seen is unmeasured. **[H]**

### 4.5 Assumptions that may be wrong

| # | assumption | why it may fail |
|---|---|---|
| A1 | The private unanswerable rate is ~2–3% like train | new industries plus the announced unseen types may raise it; public's rate is unknown |
| A2 | Question intents are a closed template set | only 4.2% of public skeletons match train verbatim; new industries bring new intents (warranty rate, commission, trade-in) |
| A3 | Improvements on dev120 transfer to public and private | measured: +0.375 on dev120 vs strongly negative precision on public |
| A4 | Most errors are LLM reasoning errors | dev120 evidence says most are definition and data-model errors (C1) and pipeline artifacts (§4.1) |
| A5 | The README end date is the true data end | `data_end` comes from README text; if a README omits it, out-of-range detection is unreliable (unverified for private) |
| A6 | Determinism on the T4/Mac carries over to the 5090 re-run | vLLM kernels, batching and fp16 vs bf16 differ across GPUs; greedy decoding is not bitwise stable across hardware or batch composition |
| A7 | One example bank built from train helps on unseen types | low lexical overlap (median Jaccard 0.46) and new schemas limit how useful retrieved examples are |
| A8 | A bigger or more capable model fixes most of it | the failure modes in §3 C1 and §4.1 are mostly deterministic semantics and pipeline issues, which model size doesn't fix |

---

## 5. Possible research directions (no commitment)

| direction | what it addresses | advantages | limitations / risks |
|---|---|---|---|
| **Free-form code generation** (LLM writes pandas over cleaned tables) | open-ended intents, unseen schemas | maximally general; already built | small models make silent semantic slips (C1); hard to verify; slow (long prompts); output varies with the prompt |
| **Typed intermediate plan / DSL** (LLM emits a structured query spec; a deterministic executor applies rulebook defaults) | C1 exactness, determinism, speed | defaults are applied once, correctly, everywhere; plans are inspectable, cheap to generate and easy to vote on | coverage is limited to the DSL; new private rules or intents fall outside it; needs a fallback |
| **Canonical semantic layer** (normalize every business into `orders/lines/products/customers/payments/stock/lots` with explicit provenance) | C2, C1 | separates "understand the files" from "answer the question"; ingestion can be tested independently with gold revenue checks | unseen industries may not fit the canonical model (repair tickets); a role mapping error affects every question for that business |
| **LLM-assisted schema mapping** (LLM proposes roles from headers, sample values and README; rules validate) | C2 on unseen layouts | handles new vocabulary (43% of public README phrases are unknown to the rule mapper, per README Status) | adds latency and nondeterminism; needs consistency checks |
| **Self-consistency / multi-program agreement** | C3 calibration | agreement is a cheap, strong confidence signal | costs N× compute; correlated errors (same wrong default) agree with each other |
| **Cross-check computations** (compute the same value two ways, e.g. line sum vs order total, or line vs order counts) | C1, C3 | catches parse and join errors without gold answers | only exists for some intents; needs per-intent design |
| **Learned or heuristic confidence model** (features: vote share, zero or empty results, type-hint match, intent family, schema coverage, novelty of the business) | C3 | directly optimizes the answer/abstain threshold for PA | trained on train only, it is overconfident on unseen businesses; needs leave-family-out validation |
| **Explicit answerability analysis** (does the schema contain the needed field? does the period overlap the data range?) | unanswerable decisions | deterministic, explainable; the period check is exact | needs the required fields per intent; a subjective-question detector is linguistic |
| **Question normalization** (typo, dialect and teen-code repair; date-expression resolution) | C4 | cheap; already partly built (`hints.py`) | ambiguous cases (`5/25`) need context such as the data range or the as-of year |
| **Fine-tuning** a small model on synthetic (question, plan/code, answer) data from train businesses | C1, C4, speed | allowed by the rules; could make a smaller model more reliable and faster | conflicts with the prompting-only scope in the proposal; synthetic questions may not cover the private distribution; risk of overfitting to the train rulebook |

Uncertain across all of these: how well any of them **degrades** when it meets a rule or schema it wasn't built for.
The ideal property is *recognizing* "I don't cover this" (→ abstain), not producing a confident wrong value.

---

## 6. Open questions and information gaps

**Can be answered now from existing artifacts or cheap experiments**
1. Why did the vLLM server stop answering partway through dev_008 (§4.1)? Check the vLLM log and the per-question
   `llm_error` fields of that kernel version.
2. Why does Kaggle-side dev_006 lack `sales_lines()` when the local ingest has it? Stale bundle, cache version,
   dependency versions or different code?
3. Measured seconds per question and tokens per question on 2× T4, and the projection to 2× 5090.
4. Precision broken down by public business and type, from the Kaggle logs, once they are saved.
5. How many public questions mention fields that the business lacks (gender, address, reason, channel)? This gives a
   rough unanswerable-rate estimate for public.
6. On train: what share of gold answers need data from before `as_of`, and how many are sensitive to `as_of`
   clipping? This tests whether the pipeline clips correctly.
7. Which train rules are actually exercised by gold answers, and how often each one is? This determines the value of
   getting each default right.

**Need organizer information or are unknowable before Round 2**
8. Private test size (N) → the per-question time budget.
9. Private industries and rulebook additions.
10. The exact Docker base image, and whether vLLM determinism holds on Blackwell under the same settings.
11. Whether the `reason` field is ever used for disqualification review. It isn't scored.

---

## 7. Key insights for future solution design

1. **The scoring target is the organizer's reference computation, not the meaning of the question.** Getting the
   rulebook defaults right, consistently, is worth more than better language understanding. **[E]**
2. **Precision is the score.** At the current precision (≈ 40% public, ≈ 73% dev120), the abstention policy changes
   the result more than any accuracy gain. A system that answers 40% of questions at 85% precision (+0.28) beats one
   that answers 90% at 60% (+0.18). **[E, arithmetic]**
3. **The public score is dominated by pipeline artifacts, not by model capability.** About 340 questions were never
   attempted, about 43 refusals were caused by a prompt note, about 29 zeros were given, and the bundle diverged from
   local. Fixing reliability comes before evaluating any modelling choice. **[E]**
4. **Generalization is mostly about schemas and rules, not about wording.** Answerability, the computation and even
   the template set depend on which fields a business has. New industries break the orders/lines abstraction.
   **[E]**
5. **Validation has to simulate novelty.** Use leave-family-out and leave-industry-out on train, plus public
   businesses from new families (dev_007/008, dev_011–014), treated strictly as *evaluation-only* signals. Don't
   trust deltas below ~0.1 on 120 questions. **[E]**
6. **Confidently wrong is the failure mode to design against.** Silent role mis-assignment, empty filters turned
   into 0, and helper notes turned into refusals all produce plausible-looking −1s. Signals such as zero/empty
   results, type mismatch, missing roles and novelty should push toward abstain. **[E]**
7. **Time budget and processing order are part of the score.** An unfinished run loses whole businesses. Writing
   incrementally plus a cost-aware order (cheap and confident questions first, or round-robin across businesses)
   bounds the loss. **[E]**
8. **Determinism across hardware is a real risk.** With the 0.03 tolerance about one standard error, dependence on
   sampling or batch composition can push a re-run outside tolerance even without any bug. **[H]**
