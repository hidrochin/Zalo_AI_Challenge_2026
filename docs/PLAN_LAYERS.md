# Kế hoạch tối ưu pipeline theo từng lớp

**Ngày:** 2026-11-12. **Nhánh:** `text2sql` tại `8206886` (cộng diff chưa commit sửa `SqlPool.setup`).
**Mục tiêu:** (1) độ chính xác trên PA, (2) tổng quát hoá sang ngành và layout chưa gặp với model ≤ 15B có thinking
(Qwen3.5-9B hoặc Gemma 4 12B), (3) dùng được làm PoC cho sản phẩm thật: trợ lý số liệu chạy on-prem cho hộ kinh
doanh nhỏ.

Số đo trong tài liệu này lấy từ các lần chạy đã có (public v5/v6 chấm bằng nhãn public, chỉ để đo; train replay;
log Kaggle A/B). Mục nào ghi "mục tiêu" là con số cần đạt, chưa đo.

---

## 0. Tóm tắt

| lớp | hiện tại | hành động chính | ưu tiên | trước 16/11? |
|---|---|---|---|---|
| L0 Ingest và store chuẩn | ingest + canon + facts + DuckDB views, trust theo giá trị, LLM map cột | **Giữ**; thêm bảng giao dịch tổng quát, mở rộng kiểm định giá trị | cao | một phần |
| L1 Rulebook thành policy | macro viết tay + gate mệnh đề; compile toàn bộ rulebook bằng LLM | **Sửa**: compile chỉ mệnh đề mới/đổi; thêm fixture tổng hợp + test bất biến | rất cao | có (phần lõi) |
| L2 Semantic model của shop | shop views (LLM), value hints, khối "Not in this shop" | **Giữ**; mỗi view kèm mô tả + dòng mẫu; năng lực shop thành dữ liệu có cấu trúc | cao | có |
| L3 Chuẩn hoá câu hỏi | Telex, lexicon, amounts, entities, 1 lần rewrite LLM | **Giữ**; bớt phụ thuộc rewrite; ghi nguồn gốc lexicon | thấp | có (dọn) |
| L4 Frame/IR và ngữ cảnh | hints, expect, entities, evidence, ExampleBank theo đáp án, nhánh nhanh | **Thêm** IR có kiểu; ngân hàng ví dụ question→SQL đã xác minh, lọc theo năng lực shop | cao | bản tối giản |
| L5 Solver | 1 SQL/lần gọi, lint + rewrite + 1 vòng sửa; thinking greedy | **Sửa**: router thinking theo độ khó; ghi `finish_reason`, `reasoning_content`; self-consistency có kiểm soát | cao | có |
| L6 Thực thi | SqlPool read-only, shadow, unit_shadow | **Giữ**; thêm round-trip SQL→câu tiếng Việt | trung bình | có thể |
| L7 Kiểm tra kết quả | expect, check_answer, invariants, contract fallback | **Giữ**; biến mọi kiểm tra thành đặc trưng cho L8 thay vì quyết định cứng | trung bình | có |
| L8 Quyết định | vote.decide, judge (unans), finalize | **Thêm** điểm tin cậy hiệu chỉnh (LOBO trên train); bớt vai trò judge | rất cao | có nếu có log train mới |
| L9 Runtime, log, Docker | pipeline.run, kaggle runner; chưa có Dockerfile/predict.sh | **Thêm** đồng hồ 6 giờ, log theo đúng mục 3.3, hai server độc lập, allowlist COPY | bắt buộc | có |
| L10 Đánh giá và dữ liệu | scorer, compare, holdout, mutate, sql-check, facts-check | **Thêm** fixture theo mệnh đề, LOBO, dữ liệu chưng cất | cao | một phần |

---

## 1. Bài toán thật và nguyên tắc thiết kế

Người dùng thật là chủ tiệm tạp hoá, nhà thuốc, cửa hàng VLXD... Họ có file xuất từ phần mềm bán hàng hoặc sổ tay
Excel, hỏi bằng tiếng Việt đời thường, và ra quyết định tiền thật từ câu trả lời. Dữ liệu phải ở lại máy họ (ràng
buộc offline + 15B của cuộc thi là mô phỏng on-prem và chi phí). Bốn hệ quả:

1. **Rulebook là metrics layer của doanh nghiệp.** Trong sản phẩm, nó là chính sách kế toán của từng chủ tiệm: có
   phiên bản, có test, có người duyệt. Model đề xuất, code kiểm chứng.
2. **Onboarding mỗi shop một lần** (đắt, kiểm chứng được, có thể hỏi lại người dùng) tách khỏi **trả lời mỗi câu**
   (rẻ, nhanh, tất định). Model nhỏ làm tốt tác vụ hẹp, kém với prompt 25k token gộp mọi thứ.
3. **Từ chối là một sản phẩm.** Trong cuộc thi là abstain; trong sản phẩm là hỏi lại hoặc "tôi hiểu thế này, đúng
   không?".
4. **Mỗi đáp án kèm bằng chứng**: luật đã áp dụng, truy vấn, các dòng đóng góp. Yêu cầu log của BTC (mục 3.3 hướng dẫn
   Docker) chính là yêu cầu giải trình của sản phẩm.

Ngành đã hội tụ về mô hình này (semantic model + bộ truy vấn đã xác minh + hỏi lại khi mơ hồ, LLM chỉ điền phần còn
thiếu). Pipeline hiện tại đi đúng hướng nhưng chưa tách lớp rõ, và phần "LLM đọc rulebook" đang được bật theo kiểu
tất cả hoặc không.

---

## 2. Kiến trúc đích

```
ONBOARDING (mỗi shop, một lần, có kiểm chứng)          TRẢ LỜI (mỗi câu, nhanh, tất định)
────────────────────────────────────────────           ───────────────────────────────────────────
file thô ─► L0 ingest ─► map vai trò ─► kiểm định       câu hỏi ─► L3 chuẩn hoá ─► L4 FRAME (IR có kiểu)
                        (LLM đề xuất,  bằng giá trị              metric · period · filters · entity · shape · k
                         code xác nhận)                          slot tất định: code; slot mơ hồ: LLM
rulebook ─► L1 diff với bản đã test ─► macro tay                        ▼
            └ mệnh đề mới ─► LLM compile ─► fixture      L5 router theo độ khó
                                            tổng hợp     ├─ nhánh nhanh (0 LLM)
README ─► L2 shop views ─► chạy + có dòng + mô tả         ├─ SQL, không thinking
                                                         └─ SQL + thinking (+ judge)
semantic model của shop = views + macros + từ điển                ▼
tên + năng lực + ví dụ đã xác minh                       L6 thực thi ─► L7 kiểm tra ─► L8 điểm tin cậy
                                                                  ▼
                                                         trả lời │ unanswerable │ abstain (sản phẩm: hỏi lại)
                                                                  ▼
                                                         L9 log bằng chứng ─► phản hồi ─► ví dụ/luật của shop
```

---

## 3. Từng lớp

Mỗi lớp: hiện trạng → vấn đề → thay đổi (Giữ / Thêm / Bớt / Sửa) → cách đo. "Điều kiện nhận" là tiêu chí để coi việc
đã xong.

### L0. Ingest và store chuẩn

**Hiện trạng.** [src/datamate/ingest/](../src/datamate/ingest/) (header thật, sheet theo tháng, file tách, tiền/ngày/
số lượng, vai trò cột từ README, forward-fill, trùng dòng), [canon.py](../src/datamate/canon.py) dựng `sales_lines`
với `product_key`/`customer_key`, [facts.py](../src/datamate/facts.py) dựng bảng fact theo grain rulebook (nợ, thanh
toán, giao hàng, nhập, chi phí, tồn, lô), [sql/store.py](../src/datamate/sql/store.py) ghi DuckDB với views và macro,
[ingest/trust.py](../src/datamate/ingest/trust.py) chấm `trusted/suspect` bằng giá trị, [agent/columns.py](../src/datamate/agent/columns.py)
để LLM đề xuất vai trò cho cột chưa map và kiểm tra hình dạng bảng, [agent/statuses.py](../src/datamate/agent/statuses.py)
phân loại trạng thái lạ một lần cho mỗi giá trị.

**Vấn đề.** Ngành mới mất điểm chủ yếu ở lớp này (phân tích public 08/11: S1 114/169 điểm mất ở dev_011–014). Shop
không có cấu trúc đơn + dòng (phiếu sửa xe, sổ giao dịch) chỉ map được một phần. `facts-check` chỉ chạy trên train;
13 câu public trả lời được nhưng fact không được phát hiện (dev_013 giao hàng, dev_014 nợ/tồn).

**Thay đổi.**
- Giữ toàn bộ. Đây là phần có giá trị lâu dài nhất và đã được kiểm trên 30 shop train.
- Thêm: **bảng giao dịch tổng quát** (`events`): mỗi dòng một sự kiện có ngày, tiền, đối tượng, loại; dựng khi không có
  cặp đơn + dòng. `sales_lines` dựng từ `events` khi có tiền và tên hàng. Điều kiện nhận: biz_110 (chỉ đơn) và 3 shop
  không mã đơn (biz_107/113/126) qua `sql-check` như hiện nay; bộ đột biến `mutate.py` thêm mode "bỏ mã đơn".
- Thêm: kiểm định giá trị cho **mọi fact**, không chỉ doanh thu: thanh toán ≤ tổng đơn, ngày giao ≥ ngày đặt, tồn cuối
  kỳ = tồn đầu + nhập − bán (khi có đủ ba), lô có hạn dùng sau ngày nhập. Fact không qua kiểm định → `suspect`, và
  `suspect` là đặc trưng đi vào L8 (không chặn cứng).
- Thêm: LLM map vai trò chỉ chạy cho cột còn trống **và** mọi đề xuất phải qua `trust.role_problem` (đã có một phần
  trong `_fit_table`). Ghi lý do chấp nhận hoặc từ chối vào cache để giải trình.
- Sau hackathon: cập nhật tăng dần (file mới mỗi ngày), `as_of` theo thời điểm hỏi, onboarding wizard hỏi chủ tiệm
  xác nhận 5–10 mapping không chắc.

**Đo.** `facts-check` và `sql-check` trên train không lùi; `mutate.py` (đổi tên cột, xáo, đổi định dạng, bỏ README, bỏ
mã đơn) giữ tỉ lệ khôi phục vai trò; số fact `suspect` trên public chỉ để quan sát.

### L1. Rulebook thành policy có test

**Hiện trạng.** Hai cơ chế song song:
- Macro viết tay trong store, mỗi macro gắn mệnh đề trong `rules.DEPENDS`/`TESTED` ([rules.py](../src/datamate/rules.py)).
  Mệnh đề giống bản train → bật; khác → hỏi model một lần; mất → tắt. Trên rulebook public, gate tắt
  `revenue`, `order_count`, `cancel_count`, `cancel_rate`, `avg_order_value` nếu model trả lời "đã đổi" cho "Đơn thuộc
  kỳ nào" (mệnh đề này chỉ làm rõ thêm, theo phân tích 06/11).
- `--catalog compiled` ([sql/compile.py](../src/datamate/sql/compile.py)): model đọc cả rulebook theo từng mục, viết
  macro, kiểm tra duy nhất là "chạy được trên ít nhất một shop"; đồng thời **tắt** macro tay, catalog advice, lint và
  rewrite theo rulebook, shadow check, nhánh nhanh.

**Vấn đề.** Compile không ổn định: cùng model, cùng rulebook public, A giữ 0 macro doanh thu và 29 macro ngành; B giữ
21 macro doanh thu và 0 macro ngành; cả hai 0 macro công nợ. Mỗi lần compile mất 53–60 phút trên 2×T4. Macro compile
sai thì mọi câu gọi nó sai cùng chiều, kết quả trông hợp lệ nên L7 không bắt được. Một output LLM dùng chung cho hàng
trăm câu là nguồn rủi ro tái lập lớn nhất (ngưỡng 0.03 ≈ 15 câu nếu private có 500 câu).

**Thay đổi.**
- Sửa: **compile chỉ phần chênh lệch.** `rule_gate` cho ra ba tập: *giống* (macro tay bật), *đổi nghĩa hoặc mới* (đưa
  cho compile), *không chắc* (tắt macro, solver đọc văn bản). Compile nhận đúng các mệnh đề của tập hai, kèm macro tay
  còn bật làm "đã có". Bỏ việc tắt toàn bộ lint/shadow/nhánh nhanh; chỉ tắt những gì phụ thuộc mệnh đề đã đổi (cơ chế
  `off` đã có).
- Thêm: **fixture tổng hợp theo mệnh đề** (`tests/fixtures/rules/<clause>.py`): một shop nhỏ 20–50 dòng mà giá trị đúng
  của mệnh đề biết trước theo cách dựng (3 đơn, 1 huỷ, 1 trả một phần → doanh thu = đơn còn lại; 2 lô giá khác nhau →
  giá vốn bình quân; thanh toán sau ngày mua → trả nợ). Macro compile phải cho đúng giá trị trên fixture; sai thì trả
  lại model kèm chênh lệch, tối đa 2 vòng; vẫn sai → mệnh đề về tập *không chắc*. Fixture cũng chạy cho macro tay (thay
  một phần `sql-check` khi không có gold).
- Thêm: **test bất biến** không cần gold, áp cho mọi macro: `revenue(a,b) = Σ revenue từng ngày`; `Σ share_of` theo mọi
  giá trị của cột ≈ 100; `cancel_rate ∈ [0,100]`; `debt_total(x)` không tăng khi thêm một thanh toán; `order_count ≥
  cancel_count` của cùng tập; `stock_at` không âm khi fact `trusted`. Chạy trên mọi store của split lúc khởi động
  (vài giây), macro vi phạm bị loại.
- Sửa: cache compile theo (rulebook, mệnh đề, model, phiên bản prompt) và **đóng gói cache compile của train trong
  image** để private chỉ compile phần mới; log ghi SQL của mọi macro compile đã dùng cho từng câu.
- Bớt: lời nhắc "nếu SQL của macro trái rulebook thì tự tính" trong card `rich`. Model nhỏ không làm được việc kiểm
  toán này; fixture làm thay.
- Sau hackathon: rulebook thành bảng mệnh đề có cấu trúc (khoá, văn bản, macro, fixture, phiên bản); giao diện cho chủ
  tiệm sửa định nghĩa = tạo mệnh đề mới + fixture tự sinh + duyệt.

**Đo.** Compile rulebook **train** bằng cùng model, so từng macro với macro tay trên gold (`sql-check`): tỉ lệ macro
compile qua fixture, qua bất biến, và khớp gold. Đây là số đo độ tin cậy của compile mà không cần public. Mục tiêu:
≥ 90% macro qua fixture; macro không qua không bao giờ được gọi.

### L2. Semantic model của shop

**Hiện trạng.** [sql/shopviews.py](../src/datamate/sql/shopviews.py): model đọc README + card, viết ≤ 6 view, phải
chạy và có dòng, 1 vòng sửa, cache theo shop. [sql/values.py](../src/datamate/sql/values.py): cột và giá trị đứng sau
từ trong câu hỏi, không gọi model. `absent_part` (`--context rich`): các fact store không có. `sql_card` liệt kê view,
bảng thô, macro, khoá nối.

**Vấn đề.** View chỉ được kiểm "chạy được"; model nhỏ nối câu hỏi với view qua tên cột kém hơn qua mô tả. Năng lực
shop (có nợ, có giao hàng, có lô...) nằm rải rác: `trust.states`, `absent_part`, `ready_terms`; không có một nơi để L4
lọc ví dụ và L8 tính tin cậy.

**Thay đổi.**
- Giữ shop views ở dạng bổ sung (canonical vẫn là nguồn chính).
- Sửa: mỗi view phải kèm **một câu mô tả tiếng Việt** (model viết, ghi trong `-- doc:`) và card in **3 dòng mẫu**;
  view trùng canonical bị bỏ (đã có `_copy_of`). View đọc cột free-text bằng `=` bị từ chối (dùng `free_text_lint`).
- Thêm: **hồ sơ năng lực shop** (`Capabilities`, JSON cache mỗi shop): với mỗi năng lực (doanh thu, đơn, khách, nợ,
  thanh toán, giao hàng, nhập, chi phí, tồn, lô, ca, nhân viên, kênh, giá vốn): `trusted | suspect | absent`, nguồn
  (fact, view shop, bảng thô + cột), và mệnh đề rulebook liên quan. `absent_part` và khối "Not in this shop" đọc từ
  đây; L4 lọc ví dụ theo đây; L8 dùng làm đặc trưng. Sinh từ `trust`, `build_facts`, shop views và bảng thô (một bảng
  thô có cột ngày + tiền + tên khách mà không fact nào nhận → `suspect`, không phải `absent`: đúng bài học 13 câu
  public ngày 08/11).
- Sau hackathon: từ điển tên (sản phẩm, khách, nhân viên, nhóm) thành bảng alias có thể sửa; thêm embedding nhỏ
  (≈ 0.3–0.6B, tính vào 15B) cho nối tên nếu đo được lợi ích trên train > 0.01 PA (quy tắc đã đặt ngày 07/11).

**Đo.** Trên train: hồ sơ năng lực khớp với 70 câu unanswerable gold (năng lực `absent` ↔ gold unanswerable) và không
đánh dấu `absent` năng lực nào có câu trả lời được. Trên holdout: số câu dùng view shop và đúng.

### L3. Chuẩn hoá câu hỏi

**Hiện trạng.** [preprocess/](../src/datamate/preprocess/): Telex, lexicon (viết tắt, teen-code, phương ngữ, tiếng
Anh), số tiền và mốc thời gian, tên thực thể trong shop; một lần rewrite LLM (greedy, ≤ 400 token) bị loại nếu đổi số,
mất tên hoặc đổi từ nghiệp vụ. Solver thấy cả câu gốc và câu chuẩn.

**Vấn đề.** Lớp này đã ổn (rewrite bị loại khi đổi nội dung). Rủi ro còn lại là tuân thủ: lexicon có mục lấy từ
public (bạn chọn miễn trừ; QnA không nói đến ngoại lệ này).

**Thay đổi.**
- Giữ toàn bộ luật tất định.
- Bớt: rewrite LLM chỉ chạy khi luật để lại từ chưa biết (phần dư của frame ở L4 khác rỗng); câu mà frame giải thích
  hết không cần rewrite. Tiết kiệm một call cho ~30% câu.
- Sửa: mỗi mục lexicon ghi nguồn (`train`, `tiếng Việt chung`, `public`); mục `public` loại khỏi image hoặc được bạn
  xác nhận với BTC và ghi trong README nộp.
- Sau hackathon: lexicon thành dữ liệu theo vùng miền, có thể bổ sung từ phản hồi người dùng.

**Đo.** `datamate normalize` trên train: số câu rewrite bị loại, số câu đổi kiểu đáp án do rewrite; không lùi khi bớt
rewrite.

### L4. Frame (IR có kiểu) và ngữ cảnh

**Hiện trạng.** Đã có các mảnh của một IR nhưng rời rạc: `time_hints` (kỳ), `expect_answer` (kiểu, k, tháng/ngày),
`entities` (tên), `values.hints` (cột/giá trị), `pick_rules` (dòng rulebook theo từ), `_PLAN` (3 dòng comment model tự
viết), [fast.py](../src/datamate/fast.py) (frame đóng cho 5 đại lượng contract), `ExampleBank` (TF-IDF ký tự, ví dụ
chỉ có đáp án, loại cùng shop).

**Vấn đề.** Solver nhận mọi thứ ở dạng văn bản và phải tự gộp. Lỗi lớn nhất trên train là lọc rơi rớt (ca, walk-in,
grain đơn/dòng). Ví dụ few-shot không có SQL nên không dạy được cách viết; ví dụ công nợ kéo model trả lời ở shop không
có nợ.

**Thay đổi.**
- Thêm: **`Frame`** (dataclass): `metric` (tên macro/đại lượng hoặc `other`), `periods[]`, `filters[]` (cột, giá trị,
  nguồn: câu hỏi/`values`), `entities[]` (khoá đã nối), `group_by`, `shape` (từ `Expect`), `k`, `residual` (từ chưa
  giải thích). Code điền mọi slot có thể; model chỉ điền `metric`, `filters` thiếu và `shape` khi `residual` khác
  rỗng (một call ngắn, JSON, không thinking). SQL sinh **bằng code** khi `metric` là macro bật và `residual` rỗng
  (mở rộng nhánh nhanh từ 5 lên toàn bộ catalog); ngược lại frame được in vào prompt thành khối có cấu trúc thay cho
  `_PLAN` để model viết SQL.
  - Trước 16/11: bản tối giản = in frame vào prompt + sinh SQL bằng code cho các macro đã có contract (revenue,
    order_count, cancel_*, avg_order_value, debt_*, item_stats, customer_stats). Điều kiện nhận: precision của phần
    sinh bằng code trên holdout ≥ 0.98, câu có `residual` không bao giờ vào nhánh code.
- Thêm: **ngân hàng ví dụ question → SQL đã xác minh** (`assets/examples_sql.jsonl`): từ log train mà SQL cho đúng gold
  (`export_examples` đã có cho pandas, mở cho SQL), khoá truy xuất là câu đã bỏ số và tên, loại cùng shop, **lọc theo
  hồ sơ năng lực** của shop đích (ví dụ nợ chỉ khi shop có nợ). Ví dụ in kèm frame của nó để model học ánh xạ frame →
  SQL.
- Sửa: `pick_rules` ưu tiên mệnh đề của `metric` trong frame trước, idf sau.
- Bớt: `_PLAN` tự do (ba dòng comment) khi đã có frame; giữ `-- needs:` vì log cần.
- Sau hackathon: frame là thứ hiển thị cho chủ tiệm xác nhận ("doanh thu, tháng 7/2025, trừ đơn huỷ, đúng không?").

**Đo.** Trên train: tỉ lệ câu frame giải thích hết (coverage) và precision của SQL sinh bằng code; so sánh có/không ví
dụ SQL trên holdout bằng `compare` (ΔPA, CI).

### L5. Solver

**Hiện trạng.** Một call → một SQL; `term_advice`/`grain_advice` một lần; `rewrite_macros`, `add_not_walkin`,
`rebind_key`, `lint`; 1 vòng sửa với lỗi binder hoặc cảnh báo; thinking bật bằng chat template, greedy, 8192 token;
[llm_local.py](../src/datamate/agent/llm_local.py) chỉ giữ `content`.

**Vấn đề.** Thinking bật cho mọi câu (chậm 10–15 lần, Gemma local 658 s/câu; compile 1 giờ). Greedy + thinking có rủi
ro lặp và không ghi `finish_reason` nên không biết bao nhiêu call chạm trần. `reasoning_content` bị bỏ, trong khi log
BTC bắt buộc có `thinking` đầy đủ.

**Thay đổi.**
- Thêm: **router ba tầng** theo frame và năng lực shop:

  | tầng | điều kiện | thinking | mẫu |
  |---|---|---|---|
  | code | `residual` rỗng, macro bật, năng lực `trusted` | không LLM | 1 |
  | thường | `residual` nhỏ, ngành/fact đã biết | tắt | 1 (+1 khi lint bắn) |
  | sâu | `residual` lớn, năng lực `suspect`, macro compile, hoặc tầng thường bị L7 cảnh báo | bật, ngân sách giới hạn | 3 (seed cố định) |

- Sửa: `LocalLLM.generate` trả về `(content, reasoning, finish_reason)`; ghi cả ba vào log; call chạm trần token →
  coi là thất bại của mẫu (không phải SQL rỗng).
- Sửa: tầng sâu dùng **self-consistency có kiểm soát**: 3 mẫu seed cố định (T theo khuyến nghị của model card khi bật
  thinking, không greedy), so khớp frame rồi so kết quả; đồng thuận là đặc trưng cho L8.
- Giữ lint và rewrite khi mệnh đề tương ứng còn bật (không tắt theo cờ catalog).
- Sửa prompt hệ thống: khối skeleton chỉ giữ ví dụ của macro đang bật cho shop này (giảm 2–4k token, tăng prefix
  cache hit).
- Sau hackathon: fine-tune LoRA trên cặp (frame, schema, SQL) đã xác minh (xem L10).

**Đo.** Trên holdout và dev120: PA, thời gian/câu theo tầng, tỉ lệ `finish_reason = length`; mục tiêu tầng sâu ≤ 25%
số câu.

### L6. Thực thi

**Hiện trạng.** `SqlPool` read-only, `as_of` theo biến, timeout, chặn truy cập ngoài; `sql_shadow` tính lại đại lượng
contract bằng macro; `unit_shadow` cho "bao nhiêu <đơn vị>"; macro compile và shop view là temp object trên mọi cursor.

**Vấn đề.** Vụ A/B chết ở `setup` (as_of NULL lúc bind) cho thấy temp object cần kiểm tra lúc tạo; diff hiện tại đã sửa
(bỏ qua statement hỏng, ghi stderr). Chưa có kiểm tra "SQL trả lời đúng câu hỏi này" ngoài kiểu đáp án.

**Thay đổi.**
- Giữ; commit bản sửa `setup`; thêm test cho macro table bind `as_of()` (đã có trong diff).
- Thêm: **round-trip**: với câu ở tầng sâu, một call ngắn (không thinking) dịch SQL cuối cùng thành một câu tiếng Việt;
  so khớp từ khoá tất định với frame (kỳ, tên, metric, bộ lọc). Lệch → đặc trưng cho L8, không chặn cứng.
- Thêm: ghi số dòng và 5 dòng đầu của kết quả trung gian (CTE cuối) vào log để giải trình.
- Sau hackathon: giới hạn bộ nhớ và CPU theo câu; cache kết quả theo (shop, SQL, as_of).

**Đo.** Round-trip trên train: tỉ lệ cảnh báo trên câu đúng (false alarm, mục tiêu < 3%) và trên câu sai (recall).

### L7. Kiểm tra kết quả

**Hiện trạng.** [postprocess.py](../src/datamate/postprocess.py): `expect_answer` (kiểu, k, tháng/ngày: 100% precision
trên 808 câu train), `check_answer`, `invariants` (tỉ lệ 0–100, số đếm nguyên, ngày trong kỳ, dấu, tiền quá nhỏ,
walk-in), `fit_money`/`fit_month`; cảnh báo cứng → sửa 1 vòng → vẫn còn thì abstain; `contract` thay kết quả khi shadow
lệch.

**Vấn đề.** Mọi kiểm tra là quyết định nhị phân, nên một cảnh báo sai làm mất câu đúng; `contract fallback` từng thay
một count đúng bằng revenue (dev_008). "Tiền quá nhỏ" chặn số đếm.

**Thay đổi.**
- Giữ mọi kiểm tra, nhưng **đầu ra là đặc trưng** (tên kiểm tra, mức: hard/soft, có qua vòng sửa không) đi vào L8.
  Chỉ kiểm tra có precision 100% trên train gold (kiểu, tháng/ngày, k, ngày trong kỳ) mới được chặn cứng.
- Sửa: `contract fallback` chỉ thay khi frame nói `metric` là đúng đại lượng contract và `residual` rỗng; ngược lại
  chỉ ghi đặc trưng "lệch contract".
- Thêm: kiểm tra độ lớn theo shop (đáp án tiền so với phân vị doanh thu ngày/tháng của shop; đếm so với số đơn/kỳ) làm
  đặc trưng mềm.

**Đo.** `eval-checks` trên gold: không kiểm tra cứng nào bắn trên câu đúng; trên log: recall của từng kiểm tra trên câu
sai.

### L8. Quyết định

**Hiện trạng.** `vote.decide` (đa số theo khoá đáp án, ngưỡng `min_share`/`unans_share`), `judge` (`unans`: xem lại
verdict UNANSWERABLE và câu có "doubt"; `all`: xem lại mọi đáp án), `finalize`.

**Vấn đề.** Judge chỉ chạm tới ≤ 7 câu trên public v6 (2 false unanswerable, 5 answered unanswerable) trong khi ngành
mới có 43 câu trả lời sai. Không có điểm tin cậy; quyết định là chuỗi luật.

**Thay đổi.**
- Thêm: **điểm tin cậy hiệu chỉnh** `p_correct` = logistic regression (hoặc gradient boosting nhỏ, sklearn; khai báo
  trong README nộp dù không đáng kể về tham số) huấn luyện trên log train với **leave-business-out**. Đặc trưng: tầng
  router; đồng thuận giữa mẫu (số khoá đáp án khác nhau); lint đã bắn và đã sửa; cảnh báo L7 (từng loại); năng lực
  shop (`trusted/suspect/absent`) của `metric`; câu dùng macro tay / macro compile / view shop / bảng thô; độ gần của
  frame với train (điểm TF-IDF cao nhất); kiểu đáp án; `finish_reason`; round-trip lệch; số vòng sửa. Ngưỡng trả lời:
  `p_correct > 0.5` (tối ưu PA). Ngưỡng là tham số cố định trong image.
- Sửa: judge chỉ chạy ở tầng sâu và chỉ cho verdict UNANSWERABLE / "doubt" (giữ `unans`), đầu ra là đặc trưng.
- Sửa: `decide` nhận thêm `p_correct`; unanswerable chỉ khi năng lực `absent` **hoặc** đồng thuận UNANSWERABLE với
  judge; năng lực `suspect` + model UNANSWERABLE → abstain (0) thay vì −1.
- Sau hackathon: thay abstain bằng hỏi lại (hiển thị frame, hỏi slot thiếu); ngưỡng theo sản phẩm; phản hồi người dùng
  thành nhãn huấn luyện lại bộ hiệu chỉnh.

**Đo.** LOBO trên train: PA với ngưỡng 0.5 so với luật hiện tại (`compare`, CI); đường hiệu chỉnh (reliability
diagram) theo 10 bin; trên holdout: PA và precision. Mục tiêu: precision ≥ 0.95 ở ngành cũ, ≥ 0.85 ở shop `suspect`.

### L9. Runtime, log, Docker

**Hiện trạng.** `pipeline.run` theo shop, ThreadPool 16, ghi submission tăng dần; [kaggle/run_kaggle.py](../kaggle/run_kaggle.py)
dựng vLLM (TP=2, prefix cache, seed 0), chuỗi dự phòng Gemma → Qwen, `smoke` stage (diff chưa commit). Chưa có
Dockerfile, predict.sh, đồng hồ tổng, log theo mục 3.3.

**Thay đổi (bắt buộc trước khi nộp).**
- Thêm `docker/Dockerfile`, `predict.sh`: base image có vLLM hỗ trợ sm_120 (bản gợi ý 0.10.1.1 ra trước Qwen3.5, kiểm
  tra nạp model trên 5090 thật); `HF_HUB_OFFLINE=1`; một model duy nhất trong `/code/models`; COPY theo **allowlist**
  (`src`, `assets` đã lọc, `cache/compiled/train`, weights) — không COPY `outputs/`, `debug/`, `cache/*/public_test`,
  `ground_truth.jsonl`, `assets/oracle/`.
- Thêm: **hai server vLLM độc lập** (mỗi GPU một model 9B BF16), câu chia theo shop một cách cố định (hash id shop),
  tránh NCCL/TP mà hướng dẫn đã gặp lỗi; prefix cache theo shop hiệu quả hơn.
- Thêm: **đồng hồ 6 giờ** trong `run`: ngân sách tính từ lúc `predict.sh` bắt đầu; quá 60% → tắt tầng sâu; quá 85% →
  tắt thinking và judge; còn 10 phút → ghi abstain cho phần còn lại.
- Sửa log mỗi câu theo mục 3.3: `id`, `answer`, `elapsed_ms` (int), `thinking` (list theo thứ tự call, đầy đủ, `null`
  nếu không có), `code` (SQL cuối cùng + SQL của macro compile/view shop đã gọi), `output` (kết quả thô), thêm `frame`,
  `features`, `p_correct`, `route`.
- Bớt khỏi code nộp: `--oracle`, `--keep-tables` (nội dung theo id câu/shop), chuỗi dự phòng model, các cờ thí nghiệm.
- Tái lập: chạy image 2 lần trên train (có đáp án) trên 2×5090; ΔPA và số dòng khác nhau ghi trong README nộp.
- Sau hackathon: API nội bộ (câu hỏi → frame → đáp án + bằng chứng), hàng đợi theo shop, cập nhật dữ liệu tăng dần.

**Đo.** Smoke 2 shop × 2 câu offline (`--network none`); thời gian/câu × 500 + nạp model < 6 giờ với biên 30%.

### L10. Đánh giá và dữ liệu

**Hiện trạng.** `scorer` (PA chính xác), `compare` (ΔPA, bootstrap CI), `holdout.txt` (80 câu, 5 shop train khác
style), `dev120`, `mutate.py` (đột biến dữ liệu, biến thể rulebook), `semantic-check`, `sql-check`, `facts-check`.

**Thay đổi.**
- Thêm: fixture theo mệnh đề (L1) và test bất biến; `datamate rule-check` chạy cả hai trên macro tay lẫn compile.
- Thêm: **LOBO** (`datamate lobo <run>`): huấn luyện bộ hiệu chỉnh và báo PA theo shop bị giữ lại.
- Thêm: báo cáo theo **tầng router** và theo **năng lực shop** (không chỉ theo kiểu đáp án và shop).
- Sửa: mọi so sánh giữa hai cấu hình đi qua `compare` với CI; không chọn fix theo nhãn public (quy tắc đã đặt).
- Sau hackathon, lộ trình chưng cất: sinh (câu hỏi paraphrase, frame, SQL) trên train bằng teacher, giữ SQL khớp gold
  (rejection sampling), thêm biến thể rulebook tổng hợp; SFT LoRA Qwen3.5-9B trên frame + SQL; preference optimization
  với cặp đúng/sai từ log thực thi; đo bằng LOBO và `mutate`. Đóng gói code, data, log theo mục 8 hướng dẫn Docker.

---

## 4. Lộ trình

### Trước hạn nộp (13–15/11)

Mỗi đội nộp Docker tối đa 2 lần, BTC lấy lần cuối. Lần 2 chỉ nộp khi đo được tốt hơn lần 1.

| ngày | việc | lớp |
|---|---|---|
| 13/11 | commit sửa `setup`; Kaggle chạy baseline HEAD `--catalog full` không thinking trên holdout, dev120, public (baseline sau khi gỡ tri thức public) | L6, L10 |
| 13/11 | thuê 2×5090: Dockerfile, predict.sh, hai server, đồng hồ, log 3.3, allowlist; smoke offline | L9 |
| 13–14/11 | L1 lõi: compile chỉ phần chênh + test bất biến + fixture cho 6 mệnh đề lớn (doanh thu, số đơn, huỷ, nợ một đơn, trả nợ, giá vốn); đóng gói cache compile train | L1 |
| 14/11 | L5: `finish_reason` + `reasoning_content`; router hai tầng (thường / sâu theo năng lực `suspect` và macro compile); skeleton theo macro bật | L5 |
| 14/11 | chạy image 2 lần trên train (ΔPA, thời gian) → **nộp Docker #1** (bản an toàn: catalog full + gate) | L9 |
| 14–15/11 | L4 tối giản (frame in vào prompt, ví dụ SQL lọc theo năng lực) và L8 (bộ hiệu chỉnh LOBO từ log train mới) nếu có log; đo trên holdout + ngành mới public (chỉ để đo) | L4, L8 |
| 15/11 | **nộp Docker #2** chỉ khi hơn #1 trên holdout và qua bài chạy 2 lần; chừa thời gian `docker save` + upload 30–40 GB | L9 |

Không làm trước hạn: đổi model sang Gemma 4 (chưa đo ở quy mô, prompt/lint/parser đã chỉnh theo Qwen), fine-tune,
embedding cho tên.

### Sau hackathon (PoC sản phẩm, 4–6 tuần)

1. **Tuần 1–2:** frame đầy đủ (L4) + sinh SQL bằng code cho toàn bộ catalog; hồ sơ năng lực shop (L2); bảng giao dịch
   tổng quát (L0). Đo LOBO và `mutate`.
2. **Tuần 2–3:** rulebook thành bảng mệnh đề có fixture (L1); giao diện duyệt định nghĩa; onboarding wizard với xác
   nhận mapping (L0/L2).
3. **Tuần 3–4:** hỏi lại thay abstain (L8); log bằng chứng hiển thị cho người dùng (L9); vòng phản hồi → ví dụ/luật
   theo shop (L4).
4. **Tuần 4–6:** chưng cất và fine-tune (L10); cập nhật dữ liệu tăng dần; đo chi phí/latency on-prem trên một GPU
   24–32 GB.

---

## 5. Quyết định cần chọn

1. Compile rulebook: chỉ phần chênh lệch (đề xuất) hay toàn bộ như `--catalog compiled` hiện tại.
2. Thinking: router theo độ khó (đề xuất) hay bật toàn bộ.
3. Bộ hiệu chỉnh L8: chỉ làm nếu có log train mới trước 14/11; nếu không, giữ luật hiện tại cho Docker #1 và #2.
4. Lexicon lấy từ public: xác nhận với BTC hoặc loại khỏi image.
5. Hai server độc lập thay TP=2: cần một lần thử trên 5090 để chốt.
