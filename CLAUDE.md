# Zalo AI Challenge 2026 — DataMate

Build an **offline AI agent** that answers everyday Vietnamese questions about a small
business's raw data files (CSV / XLSX / SQLite) and returns a typed answer, or declines.

Source of truth for rules: [description/](description/) (`rules.txt`, `description.txt`,
`data.txt`, `evaluation.txt`, `evaluation.png`, `score.png`). Data lives in `data/`
(gitignored; unzip `data/train.zip` and `data/public_test.zip` in place).

---

## 1. Task summary

**Input per question:** `{id, business_id, question, as_of_date}` + the business folder
`data/<business_id>/` (data files + `README.md`) + split-level `rulebook.md` and `calendar.csv`.

**Output:** one line per question in `submission.jsonl`:

| type | answer format | match rule |
|---|---|---|
| `number` | JSON number, money = integer VND | exact (float eps only) |
| `percent` | JSON number in percent (`12.4`, not `0.124`) | exact after rounding to 2 dp, sign matters |
| `string` | name exactly as in data | NFC → lowercase → non-alnum→space → collapse ws; any spelling in data **or the item code** accepted; diacritics must be right |
| `date` | `"YYYY-MM-DD"`, or `"YYYY-MM"` for month questions | exact |
| `boolean` | `true`/`false` | exact |
| `list` | ordered JSON array (even 1 element) | position-by-position, string/number rules |
| `table` | `{"columns":[...],"rows":[[...]]}` | columns by name, else by position; rows as a set |
| `unanswerable` | `{"type":"unanswerable","reason":"..."}` | correct iff reference is unanswerable |
| `abstain` | `{"type":"abstain"}` (or missing line) | 0 points |

Type must equal the reference type, otherwise wrong. Wrong *format* = wrong.

### Metric: Penalized Accuracy
`PA = (1/N) Σ s_i`, with **+1 correct, 0 abstain, −1 wrong** (answering an unanswerable
question = wrong). ⇒ Answer only when P(correct) > 0.5. **Calibrated abstention is a core
feature, not an afterthought.** Ties broken by total inference time in the organizers' re-run.

### Ranking / tie conventions
- Rank by question's criterion, else rulebook default; descending; ties by **code ascending**
  (by name if no code).
- References with a tie at the k boundary (or rank 1 for "…nhất") are **excluded** from tests.
- Fewer than k candidates → answer is all of them.

---

## 2. Hard constraints (rules.txt)

1. **Models:** open weights released **before 2026‑10‑01** with permissive licence. **Total ≤ 15B
   params across ALL models used at inference** (LLM + embedder + reranker + classifier; MoE
   counts all experts). Any quantization OK. Declare models in solution README.
2. **Fully offline** in Docker, networking disabled. No API calls, no downloads at runtime.
3. **No human in loop; no hard-coding** answers or logic keyed on question/business IDs.
4. Code generation + execution inside the container is allowed. `/data` is **read-only** →
   write temp files elsewhere.
5. Train on train data / synthetic / public data only. **Never train on public or private test.**
6. **Compute:** 2× RTX 5090 (32 GB each), 512 GB RAM. Whole private test in **≤ 6 h wall clock
   including model loading**. Blackwell → CUDA ≥ 12.8, matching PyTorch/vLLM; base image per
   organizers' Docker guideline.
7. **Reproducibility:** |PA(uploaded) − PA(re-run)| ≤ 0.03 or risk DQ ⇒ greedy decoding / fixed
   seeds / deterministic pipeline. If time limit hit, only answers already written count →
   **write `submission.jsonl` incrementally**.
8. Docker image: source (train + inference), weights, `predict.sh` reading `/data` →
   `/result/submission.jsonl`, README, and per-question logs in `/result/logs/` (≥ id, final
   answer, elapsed time; ideally generated code + execution output).

Rounds: R1 public test (leaderboard for reference) → submit frozen Docker image → R2 private
test (contains **business types unseen** in train/public). Final score = organizers' re-run.

---

## 3. Data analysis

### 3.1 Splits

| split | businesses | questions | answers | file types |
|---|---|---|---|---|
| train | 30 (`biz_101`–`biz_130`) | 3000 (40–119 per biz, mostly 118) | yes (`answers.jsonl`, with `alias`) | 107 xlsx, 20 csv, 1 sqlite |
| public_test | 14 (`dev_001`–`dev_014`) | 1000 (21–129 per biz) | no | 69 xlsx, 10 csv, 1 sqlite |
| private_test | ? | ? | no | includes **new business types** |

- `as_of_date` ≤ each business's data end date (README says "Dữ liệu có đến hết dd/mm/yyyy");
  median as-of = exactly the data end date; can be up to ~1.5 years earlier.
  Files contain records **after** as-of → must be filtered ("chưa xảy ra"), except order
  cancel/return status, which is taken as recorded on the order even if dated later.
- `calendar.csv` (2024‑01‑01 → ~2027, solar↔lunar + holidays) is identical across splits.
- `answers.jsonl`: `{id, type, answer, alias}`; `alias` lists alternative spellings + item code
  (non-empty in 167/3000). Unanswerable → `answer: null`.

### 3.2 Answer-type distribution (train, 3000)

| number | percent | list | date | string | unanswerable | boolean | table |
|---|---|---|---|---|---|---|---|
| 1884 (63%) | 578 (19%) | 193 | 145 | 88 | 70 (2.3%) | 23 | 19 |

Getting exact numeric aggregations right (with all rulebook defaults) is ~82% of the score.

### 3.3 Business types & data "families"

Industries in train: grocery (tạp hóa), coffee / bubble tea, restaurant / bia, pharmacy
(nhà thuốc, lots + expiry + active ingredient + prescription flag), building materials
(VLXD: multi-trip deliveries, credit terms, trucks), mini-mart, etc.
Public test adds **phone shop** (dev_011, dev_014: trade-ins, installments, warranty, `Ghi tắt`
abbreviations sheet), **motorbike repair** (dev_012: per-ticket log, mechanics, warranty
tickets, plate numbers), **mother & baby** (dev_013: milk number/diaper size, vouchers, points,
e-commerce channels, 2-row merged headers). Private test will add more unseen types.

The same underlying schema reappears under different "export styles" (recurring families):

| family (example biz) | shape |
|---|---|
| KiotViet-like `DanhSachHoaDon` + `DanhSachHoaDonChiTiet` + `DanhSachSanPham/KhachHang/NhapHang/ChiTietNhapHang` + `BaoCaoTonKho` (+ pharmacy `DanhSachLoHang`, `DanhSachHoaDonChiTietLo`, `DanhSachThuoc`) (biz_106/108/119/120/128) | header + detail, joined by code |
| Sapo-like `DonHang_ChiTiet` + `DanhSachQuyDoi` (biz_102/103/121/122) | one row per line, order fields only on first row of order (forward-fill) |
| "Sổ chi tiết bán hàng" (biz_112/115/116/118/130) | title rows, report period line, then header |
| hand-kept `SoBanHang` + `DanhMuc` + `TonKho` + `SoNhapHang` + `BangGia` (+ `SoGiaoHang`, `SoHanNo`) (biz_105/117) | 2-row grouped header, one sheet per month |
| flat CSV `ban_hang.csv` / `SoBanHang.xlsx` (biz_101/107/113/114/125/126) | single table, no order header |
| order-only `danh_sach_hoa_don.csv` (biz_110) | no line items at all |
| CSV relational `don_hang/chi_tiet_don/khach_hang/san_pham/thanh_toan` (biz_111/127/129) | normalized |
| `pos.sqlite` (biz_123) | 17 tables: orders, lines, payments, returns, stock card (`the_kho`), price history, lots, drug info, staff, shifts |
| mixed sources `BanHang_*` + `SoTay_*` + `ton_kho.csv` (biz_124, dev_003) | software export + hand notebook (expenses, imports) |

### 3.4 Data messiness (deliberate)

- **Column names**: shuffled order, renamed/abbreviated, mixed diacritics/ASCII/English
  (`Thanh tien`, `Amount`, `T.Tiền`, `Tiền bán`, `so_luong`, `Slg`, `S.L`).
- **Layouts**: title rows (`DANH SÁCH HÓA ĐƠN`, `SỔ CHI TIẾT BÁN HÀNG`), filter rows
  (`Trạng thái đơn hàng: Đặt hàng (Đã duyệt)`), blank spacer rows, 2-row merged headers,
  one sheet per month (sheet name = month/year), long lists split into `_1/_2` files at 100k rows
  or by year (`_2025/_2026`), or one file per month (`BanHang_T1_2025.xlsx`).
- **Dates** mixed within a column: `01/02/2025`, Excel serial `45689`, `1 thg 1 2025`,
  datetimes; dates without year → year of the sheet.
- **Money**: `55,000`, `55.000đ`, `55k`, `1,6256tr`, `45,000.0000`, plain ints.
- **Quantities** with units embedded: `3 chai`, `1 ly`; base-unit conversion via
  `DanhSachQuyDoi`/`DanhMuc`/`don_vi_sp` or README (unit with no conversion row = base, factor 1).
- **Status vocab** varies: `đã lấy`, `đã xong`, `xong`, `hủy đơn`, …; sometimes only in a
  notes column (rulebook: no status column → status in Ghi chú, else completed). Returns may be
  negative-quantity lines on the same order code.
- **Blank cells in continuation rows** = same as first row of the order (not missing, not walk-in).
- Identical duplicate rows **within one order** = export bug, count once; across orders = count both.
- README quality varies from a full data dictionary (≈8.5k chars) to a few lines.
- **Performance**: openpyxl parsing of all train XLSX took > 10 min. Use `python-calamine`
  and cache each business into a normalized format once, before answering.

### 3.5 Questions

- Everyday Vietnamese: ~26% have **no diacritics**; heavy typos (`nhaats`, `cuwar`, `ddeens`),
  slang/abbrev (`dthu`, `bnhiu`, `sp`, `z`, `ko dc`, `th7/2026`, `1/26`, `6-2025`), regional
  dialect in public (`ni`, `rứa`, `mô`, `mần`, `gồi`), emoji.
- Highly templated: a finite set of intents with paraphrases (2339 distinct digit-stripped
  skeletons over 3000 questions). Rough intent buckets (train): revenue ≈37%, inventory ≈18%,
  cancel rate/count ≈8%, customer ≈5%, debt ≈4%, import ≈4%, top-selling, price-change effect,
  shift (ca sáng/chiều), Tết window, expiry, profit, staff, deliveries.
- Example intents: revenue for day/week/month/quarter/YTD/"N ngày gần nhất"; % change vs prior
  period; top‑k products by quantity in base unit; units sold of a specific base unit ("bao nhiêu
  ly"); share of a product group / customer group; cancel rate; best day by revenue (`date`);
  monotonic growth over 3 months (`boolean`); top‑3 debtors (`table`); debt aging buckets
  (`list` of numbers); new debt vs repayments of one customer (`list`); stock at end of day;
  inventory value; slow movers (ế); price-increase effect over N days; gross/net profit;
  nearest-expiry lot; Tết vs pre-Tết.
- **Unanswerable (2.3%)**: the data lacks the needed field (gender, address, cancel *reason*,
  no inventory file, no payments/debt data, no line items, no import file), subjective
  questions ("nhân viên tay nghề cao nhất", "được khách quý mến nhất"), external knowledge
  (dosage), or period entirely outside the data range. **The same question template can be
  answerable for one business and unanswerable for another** → depends on schema, not wording.
  "No data" ≠ 0; partial-overlap periods are computed on the available part.

### 3.6 Rulebook (the most important document)

`rulebook.md` ships **per split and differs between splits**; the agent must read and apply the
rulebook of the split it is answering. public_test vs train diff:
- Added: cash-by-shift report semantics; "trung bình mỗi ngày thứ X"; superlative definitions
  (khách trung thành / sộp / hay đổi ý nhất, mặt hàng được ưa chuộng nhất, nhân viên chăm chỉ
  nhất, ngày đắt khách nhất, giờ cao điểm → `number`); "nhà cung cấp chính" (by #receipts ≠
  by money); topping-in-same-cell rule; books without status column; returns after period still
  excluded; "chưa giao" in point-in-time state.
- New industry sections: **Mẹ và bé**, **Sửa xe**, **Cửa hàng điện thoại** (trade-in,
  installment = debt, warranty rate, model/brand parsing).
- Removed from public (present in train): avg import price per month, stock in large units as
  `[big, small]`, selling price on day D.
⇒ Private rulebook will likely contain further new rules/industries. Hard-coding train rules
is insufficient; rules must be **read at inference time** (LLM in context) or the system must
be robust to them.

Key defaults (train): revenue = Σ line amounts after line discount, **no** order-level
discount, no VAT, no shipping, excluding cancelled orders **and any order with any returned
item**; orders assigned by **order date**; "bán chạy" = quantity in base units; week = Mon–Sun;
"tuần trước" = full previous week; "tháng này" = 1st → as-of inclusive, compared to same number
of days of previous month; Tết Y = 23 tháng Chạp → mùng 10 tháng Giêng; "quá/hơn/trên/dưới N"
exclusive, "từ N trở lên" inclusive; avg order value rounded half-up to đồng; other
percents/averages 2 dp; walk-in customers excluded from customer rankings; customers identified
by code if the shop has codes, else by name (spelling variants merged); debt per order =
unpaid part of the **order total** (after order discount, incl. shipping), COD and e‑commerce
orders are never debt; debt age from order date; cost = weighted average by lot, but gross
profit uses the **cost recorded on the sale line**.

---

## 4. Implications for the solution (to be decided)

- The challenge is ~two problems: (a) **robust ingestion/normalization** of messy, varied
  files into a canonical schema, and (b) **precise question → computation** following the
  rulebook, plus (c) **abstain/unanswerable decisions**.
- With ≤ 15B params total, a single ~14B code-capable instruct LLM (quantized, vLLM, both
  GPUs) is the likely ceiling; candidates must be verified for release date (< 2026‑10‑01) and
  licence.
- Time budget: 6 h for an unknown number of private questions (public = 1000; train = 3000).
  At 3000 questions → ~7 s/question including data loading; batch questions per business and
  cache normalized data per business.
- Determinism: temperature 0, fixed seeds, deterministic code execution, stable sorting by code.
- Validation: train has gold answers → build a local scorer implementing the exact PA metric
  and string normalization; use leave-business-out evaluation to simulate unseen businesses.

---

## 5. Repository

- **Plan of record:** [docs/PROPOSAL.md](docs/PROPOSAL.md). It covers the hybrid architecture,
  prompting only, a solo developer with under 2 weeks, and a Claude example bank built from
  train only.

- **Tasks and run commands:** [README.md](README.md). Its "Status" section is the handoff point
  between sessions; read it first.
- **Research plan (2026-11-04):** [docs/PROPOSAL_V3.md](docs/PROPOSAL_V3.md) (Vietnamese, for the mentor):
  pandas code generation + a tested semantic layer + result validation; experiments E0–E5. Fine-tuning is now
  an option, so "prompting only" and "solo" in PROPOSAL.md no longer bind.
- **Current plan (2026-11-05):** [docs/PLAN_TEXT2SQL.md](docs/PLAN_TEXT2SQL.md): fast branch + text-to-SQL on DuckDB.
  pandas code generation is paused; Docker and vLLM work is deferred.
- **Status (2026-11-07, evening):** read the README "Status (2026-11-07, evening)" section first.
- **Earlier status (2026-11-05):** README "Status (2026-11-05, evening)". E0 ran code older than
  `98d4c7b`, so its 0.473 is not comparable with the public 0.288.
  - Local model: Qwen3.5-9B on Ollama (alias `qwen3.5-9b-24k`, 24k context, thinking off).
  - Public leaderboard: PA −0.110 → 0.040 → **0.288** (pipeline `98d4c7b`, precision ≈ 68%).
  - E0, all 3000 train questions on Kaggle: **PA 0.473** (precision 77.7%), logs in
    `outputs/kaggle/result_train/out/train`.
  - Kaggle runner in `kaggle/`: `DM_STAGES=public|dev120|train DM_KERNEL=<name> kaggle/push.sh update`.
  - The prompt is about 13–16k tokens, so a local server needs ≥ 24k context (set `num_ctx` through
    a Modelfile).
- **Layout:**
  - `src/datamate/ingest/`: raw files → cleaned tables + schema card, cached in `cache/ingest/`
  - `src/datamate/dm.py`: helpers available to generated programs
  - `src/datamate/preprocess/`: question cleanup (Telex, abbreviations/dialect, amounts/dates, shop names)
    + one LLM rewrite
  - `src/datamate/postprocess.py`: answer expectations and checks, official output format
  - `src/datamate/facts.py`: fact tables at the rulebook's grain (debt, payments, trips, imports, expenses, stock, lots), found by
    README description, header or value shape; the `dm` helpers and SQL macros read them, `datamate facts-check` tests them on gold
  - `src/datamate/agent/`: prompt, retrieval, local and Claude backends, sandbox, vote, pipeline, query lint (`lint.py`), model column mapping (`columns.py`)
  - `src/datamate/eval/`: exact PA scorer (`scorer.py`), paired run comparison (`compare.py`), offline
    answer-check eval (`checks.py`), semantic contract tests of `dm` primitives (`semantic.py`)
  - `src/datamate/sql/`: entity dimensions (`dims.py`), per-business DuckDB views and macros (`store.py`), contract
    replay (`check.py`); not wired into the pipeline yet
  - `src/datamate/cli.py`: `datamate ingest | run | score | compare | eval-checks | semantic-check | sql-build |
    sql-check | facts-check | normalize | validate | export-examples`
- Bump `dm.SEMANTIC_VERSION` whenever a primitive's definition changes, and re-run `datamate semantic-check`.
- Bump `STORE_VERSION` in `sql/store.py` whenever a view, macro or dimension changes, and re-run `datamate sql-check`.
- **Commands:**
  - `uv run pytest -q`: unit tests
  - `uv run datamate ingest --split train`: writes `outputs/ingest/<split>/<biz>.md` cards
  - `uv run datamate run --backend local --model <ollama/vLLM model> --limit N`: scores on train
- `src/datamate/rules.py` gates fixed definitions on rulebook clauses; `src/datamate/fast.py` is the fast branch; `ingest/trust.py` checks roles by value.
- Bump `INGEST_VERSION` in `ingest/business.py` whenever ingestion output changes, so the caches
  rebuild.
- Python ≥ 3.11 managed with **uv** (`uv sync`, `uv run ...`, `uv add <pkg>`).
- Current deps: pandas, numpy, openpyxl, python-calamine, pyarrow, tqdm; dev: ipykernel.
- `data/`, zips, `.venv/`, caches and outputs are gitignored.
- Keep scratch/exploration scripts out of the repo root unless they become real code.
