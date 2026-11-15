# Kế hoạch tối ưu pipeline theo từng lớp

**Ngày:** 2026-11-12. **Nhánh:** `text2sql` tại `8206886` (cộng diff chưa commit sửa `SqlPool.setup`).
**Mục tiêu:** (1) độ chính xác trên PA, (2) tổng quát hoá sang ngành và layout chưa gặp với model ≤ 15B có thinking
(Qwen3.5-9B hoặc Gemma 4 12B), (3) dùng được làm PoC cho sản phẩm thật: trợ lý số liệu chạy on-prem cho hộ kinh
doanh nhỏ.

Số đo trong tài liệu này lấy từ các lần chạy đã có (public v5/v6 chấm bằng nhãn public, chỉ để đo; train replay;
log Kaggle A/B). Mục nào ghi "mục tiêu" là con số cần đạt, chưa đo.

**Cập nhật 2026-11-14:** kết quả đo đầu tiên của các lớp đã code (train route trên Kaggle, public trên server thuê
2×4090) nằm ở [mục 6](#6-kết-quả-đo-2026-11-14). **Đề xuất sửa sau khi đo** (đơn vị rủi ro là shop, không phải câu;
đối soát shop không cần gold; vá store bằng LLM có kiểm chứng; tắt compile macro) và **hướng dẫn debug theo lớp** nằm ở
[mục 7](#7-đề-xuất-tối-ưu-sau-kết-quả-đo-2026-11-14-tối). Kết quả sau khi sửa theo mục 7 (public_c 0.577) ở [mục 6.5](#65-lượt-public_c-mọi-sửa-của-mục-7-2026-11-14-tối); việc tiếp theo, xếp theo nguồn T/R/P, ở [mục 8](#8-bước-tiếp-theo-sau-public_c-2026-11-14-đêm). Các mục 0–5 giữ nguyên để so.

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

---

## 6. Kết quả đo (2026-11-14)

Code: `text2sql` `1e9b3cf` cộng các sửa chưa commit ghi dưới đây. Model Qwen3.5-9B, cấu hình B: `--thinking route
--max-tokens 2048 --catalog delta --judge unans --context rich --frame --rewrite residual`. Public chấm bằng
`ground_truth.jsonl`, chỉ để đo, không dùng để chọn sửa.

### 6.1 Tổng quan

| lần chạy | nơi chạy | thời gian | PA | đúng / sai / abstain |
|---|---|---|---|---|
| train route (`hydrochiii/datamate-route-train`) | Kaggle 2×T4 | 7,6 h | **0.9707** (in-sample) | 2941 / 29 / 30 |
| public B (`outputs/rented/public_b`) | vast.ai 2×4090 | 28 + 24 phút | **0.406** | 664 / 258 / 78 |
| public v5 (`fd767ea`, để so) | Kaggle | | 0.751 | 836 / 85 / 79 |

Train là in-sample: macro, luật và ví dụ đều dựng từ train, nên 0.97 không dự báo được public hay private.
Public B: shop đã biết 0.618 (dev_004/006/007/008 hơn v5), shop mới −0.390. Sập: dev_003 −0.527, dev_009 0.143,
dev_011 −0.275, dev_012 0.020, dev_013 −0.684, dev_014 −0.524.

### 6.2 Theo lớp

**L0 (nguyên nhân chính của 0.406).** Store trên Mac và trên server cho cùng số; dev_003 doanh thu Tết 87.664.600 với
code hiện tại, v5 và gold 84.059.600. Nguyên nhân là commit tuân thủ `7ca2324` gỡ phần ingest chỉ public có (SoTay,
trạng thái trong ghi chú, sheet viết tắt, quy đổi trong ô, shop chỉ có đơn...). Đây là lần đo public live đầu tiên
sau commit đó. Sửa lại chỉ được khi train có ca tương ứng.

**L1 (compile delta).** Ba lỗi, hai đã sửa:
- Cả 9 section trả reply rỗng (thinking dùng hết 8192 token, `finish_reason = length`), nên 0 macro. Sửa: `_think`
  trong `pipeline.py` hỏi lại một lần với thinking tắt; bản ghi compile lưu `finish_reason`, `thinking_chars`, `cut`;
  `COMPILE_VERSION = 3`.
- Sau sửa: 10 macro qua kiểm tra trên store, 9 hỏng do SQL sai (bảng/cột không có, subquery 2 cột). Mẹ và bé và Cửa
  hàng điện thoại vẫn `length` khi đã tắt thinking: model lặp, section điện thoại viết `phone_model_lines` 12 lần.
  **Chưa sửa:** `parse_macros` không bỏ macro trùng tên trong cùng một reply.
- Compile chạy tuần tự (section sau nhận macro của section trước), 1 request mỗi lúc: khoảng 21 phút trên 2×4090.
- Tác dụng lên câu trả lời gần như không có: trong lượt chạy lại 176 câu, 4 câu gọi macro compile, không câu nào đúng.

**L5 (router).** `route_reasons` không bao giờ thấy "changed rule" (0/1000) vì `pick_rules` trả dòng dạng
`"[section] line"` còn delta giữ dòng trần. Đã sửa; 336 câu public chạm 25 dòng luật mới nay đi tầng sâu. Trước sửa
552/1000 câu đã đi tầng sâu.

**L8 (quyết định).**
- Logistic LOBO trên log train route: không bỏ câu nào, trần +0.0097. Trên lần chạy này L8 học được là vô dụng
  (train quá sạch).
- Cổng tất định đo trên train (chưa code):

  | cổng | train |
  |---|---|
  | hỏi số lượng mà đáp số ≥ 1e6 | 4/4 sai |
  | hỏi "bao nhiêu món" mà trả list → `len()` | 4/4 thành đúng |
  | trả table khi câu không có từ chỉ bảng | 5/5 sai |
  | dòng UNANSWERABLE sau SQL trong khối hỏng | 3/3 gold unanswerable |

  Thêm: 8/29 câu sai do `answer_type` của rewrite LLM; 14/30 abstain là gold unanswerable.
- **Cổng shop** (mới, chưa commit; `datamate shop-gate-fit`, `datamate shop-gate`, `assets/shop_gate.json`): shop có
  tỉ lệ abstain hoặc tỉ lệ đọc bảng `raw_` vượt mức cao nhất của 30 shop train (0.042; 0.144) → mọi câu của shop
  abstain.

  | | PA public | shop bị cờ |
  |---|---|---|
  | public B, không cổng | 0.406 | |
  | public B + cổng | 0.553 (precision 0.94) | dev_003, 009, 011, 012, 013, 014 |
  | public B gộp lượt chạy lại 176 câu + cổng | 0.516 | dev_013 lọt (tỉ lệ tụt dưới ngưỡng) |

  Train: 0 shop bị cờ. Cảnh báo: tín hiệu được chọn sau khi đã biết shop nào hỏng, nên con số public lạc quan; ngưỡng
  bằng đúng max train nên dễ vỡ (ví dụ dòng cuối). Chưa nên dùng cho private khi chưa có tín hiệu ổn định hơn.

**L9 (runtime).** `remote/serve_vllm.sh` và `remote/run_parallel.sh` (chưa commit). vLLM TP=2 bf16 trên 2×4090: 52–54
request đồng thời, khoảng 1400 token sinh/s, prefix cache hit 93%, KV 729k token. Thời gian bị chặn bởi phần tuần tự,
không bởi GPU:
- prepare 28 phút trên public (compile + chuẩn bị từng shop, tuần tự);
- trong một tiến trình `datamate run` các shop chạy lần lượt; `run_parallel.sh` chạy nhiều nhóm shop song song.
  Sửa 2026-11-14: khi có `--ids-file`, chia nhóm theo số câu thật sự chạy (trước đó theo cả shop, nhóm lệch 18 vs 41
  câu, vLLM chỉ dùng 35 slot).

### 6.3 Lượt chạy lại có chọn (debug, 176 câu)

Chọn trên public B: 151 câu sai/abstain chạm dòng luật mới + 25 câu đúng ngẫu nhiên (seed 0) làm đối chứng
(`outputs/rented/delta_ids.txt` → `outputs/rented/delta_b`). Chạy 36,5 phút, trong đó prepare 23 phút (compile).

| nhóm | n | trước | sau |
|---|---|---|---|
| sai/abstain | 151 | −105 | −91 (8 thành đúng, 15 sai → abstain, 12 abstain → sai, 87 vẫn sai) |
| đối chứng | 25 | +25 | +25 |

Gộp vào public: 0.406 → 0.420. 87 câu vẫn sai gần như đều ở dev_003, 011–014, khớp chẩn đoán L0.

### 6.4 Ưu tiên tiếp theo

1. L0: tìm trong train các ca giống phần ingest đã gỡ ở `7ca2324`; chỉ khôi phục phần có ca train.
2. L8: code bốn cổng tất định ở 6.2 (đo trên train, không trên public).
3. L1: bỏ macro trùng tên trong một reply; tăng ngân sách token khi hỏi lại không thinking; đo compile rulebook train
   so với macro tay (mục L1 "Đo").
4. L9: Docker; song song hoá prepare theo shop.

Danh sách này được viết lại thành lộ trình có điều kiện nhận ở [mục 7.4](#74-lộ-trình-đến-hạn-nộp); mục 7 thay cho
mục này.

### 6.5 Lượt public_c: mọi sửa của mục 7 (2026-11-14, tối)

Server mới (vast, 2×4090, cổng 10102). vLLM: 128 seqs, 16384 batched tokens, util 0.92 (32768 bị hết bộ nhớ GPU).
Cấu hình của public B, cộng thêm: P4 (không compile), P5 (cổng), vai trò README `order_goods`, ShopAudit + cổng
`shop_suspect`, P1b (fill §8), ingest 23. Chạy 54 phút (prepare 7 phút), 1000/1000 câu, `outputs/rented/public_c`.

| | PA | đúng / sai / abstain | precision |
|---|---|---|---|
| public B | 0.406 | 664 / 258 / 78 | 0.72 |
| public_c trước cổng | 0.425 | | |
| **public_c** | **0.577** | 633 / 56 / 311 | 0.92 |

- Shop cũ dev_001–010: 0.618 → 0.723. dev_009: 0.143 → 1.000 (`order_goods`, 9 câu sai thành đúng).
- Shop mới dev_011–014: −0.390 → +0.029, gần như toàn bộ do abstain.
- Audit gắn cờ dev_003, 011, 013, 014 (giống đo offline). dev_012 vẫn clean, nên vẫn trả lời (+0.04).
- Cổng (`datamate gate`, phát lại từ trường `from`):

  | cổng | đúng → abstain | sai → abstain | ròng |
  |---|---|---|---|
  | `shop_suspect` | 30 | 192 | +162 |
  | `length` | 17 | 4 | **−13** |
  | `table_words` | 0 | 2 | +2 |
  | `count_money` | 0 | 1 | +1 |

- Cổng `length` âm trên train (−14, mục P5) và cũng âm trên public_c. Train đủ làm căn cứ bỏ cổng này. Bỏ thì public_c
  là 0.590.
- P1b không cứu dev_003: trước cổng vẫn −0.566. Phần mất của dev_003 nằm ở `status_in_text` và `discount_unapplied`,
  hai cờ mà audit đã bắt.
- Log của lượt này là code trên server lúc chạy: chưa có `log_summary` (`answer`, `elapsed_ms`, `thinking`, `code`,
  `output` ở đầu file) và trường `audit`. Code local đã có cả hai.

---

## 7. Đề xuất tối ưu sau kết quả đo (2026-11-14, tối)

Mục này đọc lại log của hai lượt chạy ở mục 6 (public B 1000 câu, train route 3000 câu) theo từng tín hiệu mà pipeline
đã ghi, rút ra lỗi nằm ở đâu, rồi đề xuất sửa. Public chỉ dùng để **đo**; mỗi đề xuất ghi rõ nguồn bằng chứng:
**T** = train có ca, **R** = suy từ cấu trúc rulebook/README/bài toán, **P** = chỉ quan sát thấy trên public (public chỉ
được gợi *cơ chế*, không được cho *mẫu* hay *ngưỡng*; mục P là quyết định của bạn theo quy tắc QnA 11/11).

### 7.1 Đọc lại số đo: lỗi nằm ở shop, không ở câu

Precision = đúng / (đúng + sai) trên các câu đã trả lời. Public B tách thành 8 shop kiểu đã gặp và 6 shop kiểu mới.

| nhóm | n | đúng | sai | abstain | PA | precision |
|---|---|---|---|---|---|---|
| public B, 8 shop kiểu đã gặp (dev_001/002/004/005/006/007/008/010) | 640 | 588 | 35 | 17 | **0.864** | 0.944 |
| public B, 6 shop kiểu mới (dev_003/009/011/012/013/014) | 360 | 76 | 223 | 61 | **−0.408** | 0.254 |
| train route (in-sample) | 3000 | 2941 | 29 | 30 | 0.971 | 0.990 |

Precision theo tín hiệu đã ghi trong log (`route.tier`, `frame`, `near`, `attempts`):

| tín hiệu | train | public 8 shop cũ | public 6 shop mới |
|---|---|---|---|
| tầng `normal` | 0.99 | 0.96 | **0.17** |
| tầng `deep` | 0.98 | 0.93 | 0.30 |
| frame có `metric` (macro tay) | 0.99 | 0.98 | **0.12** |
| frame không có `metric` | 0.99 | 0.92 | 0.27 |
| SQL đọc bảng `raw_` | 0.96 | 0.79 | 0.47 |
| SQL chỉ đọc view canonical | 0.99 | 0.95 | 0.17 |
| `near` < 0.5 (câu lạ so với train) | 0.94–1.00 (n=40) | 0.88–0.92 (n=50) | 0.24 |
| reply bị cắt `length` | 0.89 (n=18) | – | 0.12 |
| kiểu gold `date` / `boolean` / `unanswerable` | 1.00 / 1.00 / 1.00 | 0.97 / 1.00 / 0.89 | **1.00 / 1.00 / 0.88** |
| kiểu gold `number` / `percent` | 0.99 / 0.99 | 0.95 / 0.97 | 0.21 / 0.09 |

Tỉ số đáp án / gold của các câu số bị sai (chỉ để chẩn đoán, lấy từ `ground_truth.jsonl`):

| shop | câu số sai | tỉ số hay gặp nhất | đọc là |
|---|---|---|---|
| dev_003 (cà phê, SoTay + topping trong ô) | 76/84 | 1.04 (15), 1.05 (11), 1.02–1.03 (8) | store cộng dư ≈ 4–5% ở mọi đại lượng tiền |
| dev_013 (mẹ và bé, trạng thái trong Ghi chú) | 40/46 | 1.01–1.04 (16) | đơn huỷ (909/31 987 dòng ghi `cancel`/`Huỷ` ở Ghi chú) không bị loại |
| dev_009 (chỉ đơn, như biz_110) | 9/9 | 0.99 (6) | cột `Giá trị hàng` không có vai trò dù README mô tả **cùng câu** với `Tien hang` của biz_110 |
| dev_011/014 (điện thoại) | 20/24, 35/40 | rải (0.12–1.9) | parse model/hãng, "máy không tính phụ kiện", sheet `Ghi tắt` |
| dev_012 (sửa xe) | 10/20 (8 câu string sai) | rải | tên thợ viết tắt (`A.An` ↔ `Đàm Lan An`), lượt xe |

Kết luận rút ra:

1. **Pipeline theo câu đã tốt.** Trên 8 shop kiểu đã gặp, public B hơn v5 (0.864 so với 0.848) dù đã gỡ tri thức
   public. Toàn bộ khoảng cách 0.751 → 0.406 nằm ở 6 shop mới: v5 đạt 0.578 ở đó (261 đúng / 53 sai / 46 abstain) nhờ
   ingest viết theo public, B đạt −0.408. Không nên tiêu thời gian còn lại vào prompt, router hay judge.
2. **Shop mới sai hệ thống, không sai ngẫu nhiên.** Cùng một lệch hằng số (4%, 3%, 1%) xuất hiện ở hầu hết câu số của
   một shop. Giá trị trông hợp lệ nên L7 không bắt; macro tay đúng nghĩa nhưng chạy trên store sai (frame có `metric` mà
   precision 0.12); L8 theo câu không tách được (tầng `normal`, không tín hiệu nào, precision 0.17).
3. **Tín hiệu theo câu không nhận ra shop hỏng.** `near` đo câu lạ, không đo shop lạ (train 0.94 ở `near` < 0.5). Lấy
   `near` hay tầng `deep` làm cổng abstain sẽ mất câu đúng ở shop tốt mà không cứu shop hỏng.
4. **Kiểu đáp án `date`, `boolean`, `unanswerable` sống sót trên shop hỏng** (12/12, 3/3, 7/8): lệch hằng số không đổi
   ngày tốt nhất hay chiều so sánh. Kiểu `number`/`percent`/`string`/`list`/`table` thì gần như chắc sai.
5. **Compile macro gần như không có tác dụng:** 21 macro (12 trùng tên `phone_model_lines`), 4 lượt gọi, 0 đúng, mất
   21–28 phút prepare và là đầu ra LLM dùng chung lớn nhất (rủi ro tái lập).
6. **Có ca train hậu thuẫn cho ít nhất một shop hỏng:** dev_009 và biz_110 có README mô tả cột bằng cùng một câu; vai trò
   hiện gán theo header (`Tien hang` khớp, `Giá trị hàng` không) thay vì theo mô tả README. Đây là lỗi tổng quát hoá
   của ingest, sửa được bằng train (T).

### 7.2 Nguyên tắc chọn hướng: cơ chế, không phải mẫu

- **Đơn vị rủi ro là shop.** Mọi quyết định mới (route, abstain, vá) được tính ở mức shop trước, mức câu sau.
- **Chỉ thêm cơ chế kiểm định được mà không cần gold:** đối soát giá trị giữa các bảng, đối soát với README, bất biến.
  Ngưỡng lấy từ 30 shop train (0/30 shop train bị cờ là điều kiện nhận), không lấy từ public.
- **LLM đọc README + rulebook của split và viết, code kiểm chứng** (đúng hướng của mentor), nhưng viết ở **mức store**
  (view `sales_lines` của shop), nơi mất điểm, thay vì mức macro truy vấn, nơi đo được là vô dụng.
- **Kiểm chứng bằng `mutate` trên train:** biến một shop train thành "kiểu xuất khác" (trạng thái vào ghi chú, tiền trước
  giảm, đổi header, bỏ vai trò) rồi đòi cơ chế mới (a) phát hiện và (b) khôi phục đáp án gold. Đây là cách đo tổng quát
  hoá duy nhất không chạm public.
- **Mọi output LLM dùng chung cho nhiều câu** phải: greedy, cache theo khoá, qua kiểm chứng có gold (mutate) hoặc bất
  biến, và có đường lùi là abstain.

### 7.3 Đề xuất theo lớp

#### P1 (L0 + L2). Đối soát shop không cần gold: `ShopAudit`

Chạy trong prepare, không gọi model, vài giây mỗi shop. Kết quả `audit.json` cho mỗi shop: danh sách cờ, mức
`sales_bias ∈ {clean, suspect}`, và nhập vào `Capabilities` (L2) làm trạng thái của năng lực `revenue/orders/products`.

| cờ | cách tính | kỳ vọng train | nguồn |
|---|---|---|---|
| `role_gap` | dòng README mô tả một cột mà mô tả đó **khớp mô tả của một cột đã có vai trò ở train** (từ điển mô tả → vai trò dựng từ 30 README train, so bằng `fold` + bỏ dấu câu) nhưng cột trong shop này không được gán vai trò | 0 | **T** (biz_110 ↔ dev_009) |
| `status_in_text` | shop không có đơn huỷ/trả sau chuẩn hoá (`status` toàn `hoàn thành`) **nhưng** một cột text không có vai trò tên/mã (bỏ `customer_name`, `product_name`, nhà cung cấp...) có ≥ 0,5% ô mà **cả ô** là một cụm trạng thái `normalize_status` đã biết (`huỷ`, `đã huỷ`, `huỷ đơn`, `cancel`, `trả hàng`, `hoàn trả`, `đang giao`, `chưa giao`) | **đo 14/11 trên cache ingest train: 0/30**. Luật thô "ô *chứa* từ huỷ/trả" cờ nhầm 25/30 shop vì tên khách `Huy`, `Trà` → bài học cho mọi cờ: thử trên 30 shop train trước | **R** (rulebook train §8 nói trạng thái áp cho cả đơn; từ vựng là của train) + P (rulebook public có mệnh đề "Sổ không có cột trạng thái") |
| `discount_unapplied` | bảng bán có cột giảm giá dòng (vai trò `line_discount` hoặc header/README nói "giảm") và `qty × unit_price = amount` trên ≥ 95% dòng có giảm > 0 → amount là **trước** giảm | 0 (rulebook train: doanh thu = sau giảm dòng) | **R** |
| `orphan_money_table` | bảng thô có cột ngày + cột tiền ≥ 100 dòng mà không fact nào, view shop nào, cột `sales_lines` nào đọc | ghi số; không cờ `suspect` một mình | **R** (đã nêu ở L2) |
| `name_gap` | tỉ lệ tên hàng trên dòng bán **không có** trong danh mục sản phẩm (khi shop có danh mục) > 5% | 0 | **R** (string answer chấp nhận mọi cách viết trong data: tên trên dòng khác danh mục là dấu hiệu tên ghép/viết tắt) |
| `alias_sheet` | sheet nhỏ (≤ 5000 dòng) có hai cột text, cột A phủ ≥ 80% tên trên dòng bán và một phần chỉ xuất hiện ở A | 0 | **P** (chính là `alias_map` gỡ ở `7ca2324`; cơ chế tổng quát, chỉ động lực là public → bạn quyết) |
| `fill_gap` | sau `_clean`, một bảng có cột nhóm (vai trò `order_id`, hoặc một cột text lặp theo khối như `Ghi chú` = "STT 34") mà cột cấp đơn/cấp nhóm (ngày, khách, tiền cả đơn) **còn trống** trên > 5% dòng, trong khi dòng đầu mỗi nhóm có giá trị | **đo 14/11 trên cache ingest: train 0/30**; public: dev_014 `SoNo.Trả góp` (Ngày/Khách/Máy/Giá trống 80%), `SoNo.Sổ nợ` (Ngày/Khách/Nợ trống 50%) | **R** (rulebook train §8 "ô để trống ở các dòng sau của cùng một nhóm dòng = dòng đầu nhóm"); ca kích hoạt là public → bạn đã duyệt 14/11 |
| `value_problems`, `trust` | như hiện có | như hiện có | T |

Luật gộp: `suspect` khi có `role_gap`, `status_in_text`, `discount_unapplied`, `fill_gap`, hoặc (`name_gap` và
`alias_sheet`); `orphan_money_table` chỉ là đặc trưng. Thêm vào `mutate.py` ba mode mô phỏng kiểu xuất khác trên shop train:
`status_in_notes` (bỏ cột trạng thái, ghi `huỷ`/`trả hàng` vào Ghi chú của dòng đầu đơn), `amount_before_discount`
(cộng lại giảm dòng vào amount, thêm cột giảm), `drop_readme_role` (đổi header thành tên lạ nhưng giữ dòng README).

**Điều kiện nhận.** 0/30 shop train bị `suspect`; mỗi mode mutate làm ≥ 90% shop bị cờ đúng loại; `pytest` xanh.
Nếu một shop train bị cờ: hoặc ngưỡng sai, hoặc tìm được lỗi ingest thật (kiểm bằng `sql-check` của shop đó).

#### P1b (L0). Fill ô trống theo nhóm dòng đúng câu rulebook §8 (đã duyệt 14/11)

Hiện trạng ([ingest/business.py](../src/datamate/ingest/business.py), `_clean` → `_forward_fill`, `_group_fill`): chỉ fill
khi bảng có vai trò `order_id`; `_forward_fill` lo dòng có mã đơn trống (kiểu dev_005, 3 shop train họ Sapo);
`_group_fill` lo cột cấp đơn trống lác đác nhưng **chỉ xét cột có ≥ 2 giá trị trong một đơn**, nên kiểu "mã đơn ở mọi
dòng, thông tin đơn chỉ ở dòng đầu" (dev_014 `Trả góp`) bị bỏ qua; bảng không có cột mã mà nhóm theo khối (dev_014
`Sổ nợ`, số đơn nằm trong `Ghi chú`) không fill. Bảng không có mã đơn (danh mục hàng, khách) không bao giờ fill → ô trống
`Mã barcode` của dev_001 giữ nguyên, đúng. Ô trống ở **dòng đầu** nhóm không fill (ô thiếu thật, không phải khách lẻ).
Chưa có unit test riêng cho hai hàm fill.

Thay đổi:
- Sửa `_group_fill`: trong nhóm theo `order_id`, cột mà hầu hết nhóm nhiều dòng (≥ 95%) có **đúng một** giá trị không
  trống và giá trị đó ở dòng đầu nhóm → fill cả nhóm. Giữ điều kiện cũ (≥ 2 giá trị thì phải nhất quán 99%). Cột
  dòng hàng (qty, line_amount, product_name) không bao giờ fill (loại theo vai trò).
- Thêm nhóm theo khối cho bảng **không có** `order_id`: khối = dãy dòng liên tiếp từ một dòng có ngày đến dòng có ngày
  kế tiếp, chỉ khi một cột lặp giá trị đúng theo khối (như `Ghi chú` = "STT 34") hoặc README nói "dòng đầu mỗi đơn ghi
  ..., mỗi lần sau một dòng riêng". Fill ngày, khách, tiền cả đơn của khối; ghi `note:`.
- Card: với mỗi cột đã fill mà vai trò là tiền cấp đơn (`order_total`, `order_discount`, `Khách trả`, `Tổng phiếu`), chú
  thích thêm "giá trị của cả đơn, lặp trên mọi dòng: cộng theo đơn (`DISTINCT order_id`), không cộng theo dòng". Lint
  (`agent/lint.py`): `SUM(<cột đã fill cấp đơn>)` trên `sales_lines`/`raw_` không kèm nhóm theo đơn → cảnh báo mềm (đặc
  trưng L8), không chặn.
- Ô trống mà README nói "trống = không có" (dev_011 `Bớt`, dev_005 `Hoạt chất`): giữ NULL ở dòng đầu; fill xuống dòng
  tiếp vẫn đúng vì cùng đơn.
- Test: fixture tổng hợp 3 kiểu (mã trống ở dòng tiếp; mã ở mọi dòng + thông tin ở dòng đầu; không mã + khối theo ngày)
  dựng từ một shop train (biz_102) bằng cách **xoá** giá trị theo kiểu, đòi `_clean` trả về bảng gốc; thêm mode
  `mutate` `first_row_only` (xoá thông tin đơn khỏi dòng tiếp của biz_102/103) và đòi `sql-check` của shop đó không lùi.

Điều kiện nhận: ingest train không đổi byte (so `cache/ingest/train/*.pkl` trước/sau; `INGEST_VERSION` tăng);
`facts-check`, `sql-check` 139/139, `pytest` xanh; `fill_gap` trên train vẫn 0/30. Nguồn: **R** (câu rulebook train §8),
ca kích hoạt public.

#### P2 (L8). Quyết định theo shop × kiểu đáp án, thay cổng theo tỉ lệ abstain

Thay `shop-gate` hiện tại (chọn tín hiệu sau khi biết shop hỏng, ngưỡng = max train, vỡ khi gộp lượt chạy lại) bằng
cổng đọc `audit.json`, tất định, có từ lúc prepare:

- Shop `suspect` → mọi câu có `frame.shape` hoặc `expect.type` thuộc {`number`, `percent`, `string`, `list`, `table`}
  **và** SQL cuối chỉ đọc view canonical (`sales_lines`, `orders`, macro tay) → **abstain**. Câu `date`, `boolean`,
  verdict `UNANSWERABLE` → giữ. Câu mà SQL đọc bảng `raw_` → đi tầng sâu, giữ nếu qua L7 (precision 0.47 trên shop hỏng
  public: vẫn dưới 0,5, nên mặc định cũng abstain; bật lại chỉ khi P3 vá xong và audit sạch).
- Shop `clean` → không đổi gì so với hiện tại.
- Lý do chọn kiểu đáp án là **R** (lệch hằng số không đổi thứ tự ngày, không đổi dấu so sánh, không đổi kết luận
  "thiếu trường"); đo lại trên public chỉ để báo: nếu 6 shop mới bị `suspect`, public B 0.406 → **0.574** (cổng theo
  shop toàn phần: 0.553). Số này lạc quan vì audit chưa chạy thật trên 6 shop; con số thật đo sau P1.
- Giữ `shop-gate` theo tỉ lệ abstain/`raw_` chỉ làm **đặc trưng** cho L8, không quyết định.

**Điều kiện nhận.** Trên train: không câu nào đổi (0 shop `suspect`). Trên shop train mutate (`status_in_notes`): PA của
shop đó không âm (abstain thay vì sai), câu `date`/`boolean` vẫn đúng ≥ 95%.

#### P3 (L0 + L2). Vá store bằng LLM theo audit + rulebook của split

Đây là bản sửa thật (P2 chỉ cầm máu). Chỉ chạy cho shop `suspect`, một lần, cache theo (shop, cờ audit, băm rulebook,
model, phiên bản prompt), thinking bật, greedy. Đầu vào: card của shop (bảng thô + 3 dòng mẫu), README, các cờ audit kèm
số liệu (ví dụ "909/31 987 dòng có `cancel`/`Huỷ` ở `Ghi chú`; canonical không có đơn huỷ"), các dòng rulebook của
section "Cách ghi trong file" và section ngành (từ `rule_delta`: 28 dòng public không có trong train, 7 dòng ở "Xếp
hạng", 6 "Sửa xe", 4 "Điện thoại", 3 "Mẹ và bé", 1 "Cách ghi trong file"). Đầu ra: một `CREATE VIEW sales_lines_patch`
đọc từ `raw_*` (hoặc từ `sales_all` cộng thêm cột) theo đúng schema `sales_lines` (`SCHEMA_DOC` đã có ở `compile.py`).

Kiểm chứng theo thứ tự, hỏng ở bước nào thì trả lỗi về model một lần, vẫn hỏng → giữ `suspect` (P2 xử lý):
1. chạy được, cùng tập cột với `sales_lines`, số dòng trong ±20% của `sales_all`;
2. `hand_problems` (bất biến L1) trên store với view vá thay `sales_lines`;
3. chạy lại audit trên view vá: cờ đã nêu phải tắt, không cờ mới;
4. **trên train:** audit sạch nên bước này không chạy (vá là no-op) → `sql-check` 139/139 không đổi;
5. **trên shop train mutate** (`status_in_notes`, `amount_before_discount`): view vá phải trả lại đáp án gold của các
   contract (`semantic-check`/`sql-check` chạy trên store mutate) ≥ 95%. Đây là phép đo có gold duy nhất của P3.

Khi qua: view vá thay `sales_lines` cho shop đó (macro tay dùng luôn), audit đổi thành `clean`, P2 thả câu ra. Log mỗi
câu ghi SQL của view vá (yêu cầu log 3.3). Thời gian: 1 call có thinking mỗi shop `suspect` (2–5 phút trên 5090), chạy
song song theo shop trong prepare.

Rủi ro: một output LLM dùng chung cho ~100 câu, như compile. Khác compile ở ba điểm: chỉ chạy khi có cờ (train: không
chạy), có kiểm chứng gold qua mutate, và đường lùi là abstain thay vì trả lời sai.

#### P4 (L1). Tắt compile macro mặc định, giữ `rule_delta` dạng văn bản

- `--catalog delta` chỉ còn làm: tính 28 dòng mới/đổi → (a) router "changed rule", (b) `pick_rules` đưa dòng vào prompt
  (`--context rich`), (c) đầu vào P3. Không sinh macro. Tiết kiệm 21–28 phút prepare, bỏ 21 macro không tất định.
- Giữ code compile sau cờ `--catalog compiled` cho sau hackathon; sửa `parse_macros` bỏ trùng tên dù không dùng.
- Nguồn: **T + đo** (4/176 câu gọi macro compile, 0 đúng; 12/21 macro trùng tên).

#### P5 (L5 + L7 + L8). Cổng theo câu: chỉ những cổng đo được trên cả train lẫn public

| cổng | train (3000) | public B (1000) | quyết định |
|---|---|---|---|
| đáp án cuối lấy từ attempt bị cắt `length` → abstain | −14 (16 đúng, 2 sai) | +14 (12 đúng, 26 sai) | **bật**: reply bị cắt không đáng tin ở shop lạ; chi phí train 0,005 |
| 4 cổng tất định ở mục 6.2 (đếm ≥ 1e6, "bao nhiêu món" → `len`, table không có từ chỉ bảng, UNANSWERABLE sau SQL hỏng) | ≈ +20 theo 6.2 (4 sai → 0, 4 sai → đúng, 5 sai → 0, 3 abstain → đúng), chưa code | chưa đo | **bật** (đo offline trên log public B trước khi bật, chỉ để báo) |
| `expect.hard` và kiểu cuối ≠ kiểu expect (không phải unanswerable) → abstain | 0 đúng bị mất | 1 sai bị chặn | bật, nhỏ |
| `near` < ngưỡng → abstain | mất 48 đúng/3 sai | shop tốt mất 40 đúng | **không bật** (7.1 điểm 3) |
| tầng `deep` → abstain | precision 0.98 | 0.93 shop tốt | **không bật** |

Router: `route_far = 0.75` đưa 55% câu public vào tầng sâu (train 13%); tầng sâu median 44 s/câu so với 7,7 s, chiếm
13,5 trong 16 giờ-CPU của lượt chạy, mà precision trên shop tốt không hơn tầng thường (0.93 so với 0.96). Trước hạn:
giữ 0.75 (an toàn), nhưng đồng hồ 6 giờ (L9) phải tắt tầng sâu khi quá ngân sách. Sau hạn: đo `route_far` 0.6 trên
holdout bằng `compare`.

#### P6 (L9). Bắt buộc trước khi nộp

Như mục L9, cộng: audit (P1) và vá (P3) chạy trong prepare, song song theo shop (ThreadPool theo shop; vLLM đã chịu
52–54 request đồng thời); prepare public hiện 1689 s tuần tự, mục tiêu < 10 phút. Log mỗi câu thêm `audit` (cờ của
shop), `patched` (có dùng view vá), `gate` (cổng nào đã abstain).

### 7.4 Lộ trình đến hạn nộp

Thứ tự theo (điểm kỳ vọng trên shop mới) / (giờ làm), với điều kiện nhận để biết đã xong. Thời lượng là ước lượng cho
một người.

| thứ tự | việc | giờ | điều kiện nhận | ảnh hưởng kỳ vọng |
|---|---|---|---|---|
| 1 | P4: `--catalog delta` không sinh macro; `parse_macros` bỏ trùng | 1 | pytest xanh; prepare public < 5 phút khi không có vá | −25 phút prepare, bớt rủi ro tái lập |
| 2 | P5: 3 cổng theo câu + 4 cổng 6.2; đo offline trên log train route và public B (`datamate gate --dry-run <run>`) | 2 | train ΔPA ≥ −0.006; public chỉ báo | +0.01–0.02 |
| 3 | P1: `role_gap` (từ điển mô tả README từ train) + `status_in_text` + `discount_unapplied` + `name_gap` + `fill_gap`; 3 mode mutate; `audit.json` vào card và `Capabilities` | 4–5 | 0/30 train `suspect`; mutate ≥ 90% bị cờ đúng; `facts-check`, `sql-check` không lùi | nền cho P2, P3 |
| 3b | P1b: `_group_fill` theo câu rulebook §8 (một giá trị ở dòng đầu), khối theo ngày cho bảng không mã, chú thích card + lint cho cột tiền cấp đơn đã fill, fixture test + mode `first_row_only` | 2–3 | ingest train không đổi; `fill_gap` train 0/30; pytest xanh | dev_014 nợ/trả góp và ca tương tự ở private đọc được |
| 4 | P2: cổng shop × kiểu đáp án đọc audit; gỡ `shop-gate` khỏi đường quyết định (giữ làm đặc trưng) | 2 | train không đổi; shop mutate PA ≥ 0 | public ước 0.55–0.57 nếu audit bắt được 6 shop |
| 5 | L9: Dockerfile, predict.sh, đồng hồ, log 3.3, allowlist COPY; chạy image 2 lần trên 5 shop holdout | 4–5 | ΔPA 2 lần = 0; smoke `--network none` | **Docker #1** |
| 6 | P3: vá store cho shop `suspect` + 5 bước kiểm chứng; đo trên 3 mode mutate của 5 shop holdout | 6–8 | mutate: contract đúng ≥ 95% sau vá; train no-op | shop mới từ abstain thành trả lời; **Docker #2** chỉ khi qua |
| 7 | `role_gap` thành sửa thật: gán vai trò theo mô tả README khi header không khớp (T: biz_110) | 1–2 | `mutate rename` giữ README: vai trò `order_goods` khôi phục trên biz_110; ingest train không đổi (`INGEST_VERSION` tăng) | dev_009 và ca tương tự ở private |

Không làm trước hạn: đổi model, fine-tune, embedding tên, sửa prompt/router/judge (7.1 điểm 1).

### 7.5 Hướng dẫn debug theo lớp

Phương pháp chung (giữ quy tắc đã đặt: giả thuyết viết trước, can thiệp có đối chứng, không tự giải thích sau):

1. **Triage theo tín hiệu trước, đọc câu sau.** Với một lượt chạy có gold (train, hoặc public chỉ để đo), lập bảng
   precision theo từng tín hiệu trong log: `business_id`, `route.tier`, `route.reasons`, `frame.metric`,
   `frame.capability`, `len(frame.residual)`, `near`, `judge.action`, `expect.type`, `final.type` so với gold, SQL cuối
   có `raw_`, `attempts[].error/warning/finish_reason`, `elapsed`. Tín hiệu nào tách đúng/sai mạnh nhất là lớp cần
   xem; tín hiệu không tách (như `near` ở 7.1) thì **không** dùng làm cổng. Việc này nên thành lệnh
   `datamate triage <run> --gold <answers.jsonl>` (L10), hiện làm bằng script đọc `logs/*.json` + `scorer.is_correct`.
2. **Phân biệt lỗi hệ thống và lỗi câu.** Với câu số sai của một shop, lập histogram tỉ số đáp án / gold. Một đỉnh nhọn
   (1.04, 0.99) là lỗi store (L0) → sửa một chỗ cứu cả shop; tỉ số rải là lỗi hiểu câu (L3–L5) → sửa từng intent.
3. **Can thiệp có đối chứng.** Chọn tập câu sai + ~25 câu đúng ngẫu nhiên (seed cố định) làm đối chứng, chạy lại bằng
   `--ids-file`, so bằng `datamate compare` (ΔPA, CI, bảng chuyển trạng thái) như mục 6.3. Một sửa làm đối chứng rơi
   là sửa quá tay.
4. **Mọi ngưỡng và sửa phải có ca train.** Public chỉ cho biết *có* vấn đề; ca để sửa và số để chỉnh lấy từ train hoặc
   mutate trên train.

Theo lớp (triệu chứng → lệnh → nhìn gì → quyết định):

**L0 ingest / store.**
- Triệu chứng: tỉ số pred/gold đỉnh nhọn ở một shop; `frame.metric` có nhưng sai; `sql-check`/`facts-check` lùi.
- Lệnh: `uv run datamate ingest --split <split>` rồi đọc `outputs/ingest/<split>/<biz>.md` (vai trò từng cột, dòng
  README đi kèm, các `note:` về forward-fill, trùng dòng, đơn vị trong ô). `uv run datamate sql-build --split <split>`
  rồi mở `cache/duckdb/<split>/<biz>.duckdb` bằng `duckdb` và đối soát: `select sum(amount) from sales_lines where
  counted` so với `select sum("<cột tiền thô>") from raw_<bảng> where <ngày> <= as_of()`; `select status, count(*)
  from orders group by 1`; `select count(distinct product_name) from sales_lines` so với danh mục.
- Nhìn: cột tiền/ngày của bảng thô không có `role=`; `status__norm` toàn `hoàn thành` trong khi một cột text có từ
  huỷ/trả; tên hàng trên dòng nhiều hơn danh mục; dòng README mô tả cột mà cột không có vai trò. So card với **shop
  train cùng họ** (bảng ở mục 3.3 CLAUDE.md; dev_009 ↔ biz_110): cột nào biz train có vai trò mà shop này không.
- Ô trống (rulebook §8): trong card, cột vai trò `order_id`/`order_date`/`customer_name`/`status` còn `null=` > 0% sau
  clean là dấu hiệu fill thiếu (kiểm nhanh: mở `cache/ingest/<split>/<biz>.pkl`, với mỗi bảng in `isna().mean()` của các
  cột vai trò đó; train phải ra 0 bảng). Đối chiếu `note: forward-filled ...` với README: cột README nói "lặp lại ở mọi
  dòng của đơn" phải nằm trong note; cột README nói "trống = không có" không được fill ở dòng đầu. Cột tiền cấp đơn
  trong note (`Khách trả`, `Tổng phiếu`) → kiểm SQL của câu sai có `SUM` theo dòng không.
- Quyết định: nếu train có ca (cùng mô tả README, cùng dạng giá trị) → sửa luật ingest, tăng `INGEST_VERSION`, chạy
  `facts-check`, `sql-check`, `pytest`, và `run_mutations` trong `eval/mutate.py` (chưa có lệnh CLI; thêm `datamate
  mutate --modes rename no_readme --shops biz_110` là việc L10) để chắc vai trò vẫn khôi phục dưới `rename`/`no_readme`.
  Nếu train không có ca → không hard-code; để P1 cờ, P3 vá.

**L1 rulebook / compile.**
- Triệu chứng: 0 macro hoặc macro lạ; câu gọi macro compile sai cùng chiều.
- Lệnh: đọc bản ghi compile (`outputs/rented/compiled_<split>/macros_delta_*.json` hoặc `cache/compiled/`): từng macro
  có `finish_reason`, `thinking_chars`, `cut`, lỗi SQL; `uv run datamate rule-check --split train` cho bất biến; so
  `rule_delta` (28 dòng) với diff thủ công `diff data/train/rulebook.md data/<split>/rulebook.md`.
- Nhìn: `finish_reason = length` (thinking ăn hết token), tên macro trùng, macro đọc bảng/cột không có.
- Quyết định: theo P4, compile tắt; nếu bật lại thì chỉ khi đo compile **rulebook train** khớp macro tay trên gold
  (`sql-check`) ≥ 90%.

**L2 năng lực shop / shop views.**
- Triệu chứng: câu trả lời UNANSWERABLE sai (gold có đáp án) hoặc trả lời câu gold unanswerable.
- Lệnh: in `Capabilities` của shop (card phần "Not in this shop" / "Read with care"); trên train so với 70 câu gold
  unanswerable: `absent` ↔ gold unanswerable (bảng chéo). Shop views: đọc `cache/shopviews/<biz>` xem có `-- doc:` và
  số dòng.
- Nhìn: năng lực `absent` trong khi một bảng thô có cột cùng nghĩa (phải là `suspect`); `KINDS` thiếu từ khoá cho ngành
  mới (chỉ thêm từ có trong train hoặc rulebook của split đang chạy, đọc lúc chạy).
- Quyết định: `absent` sai → sửa regex theo header/README của train; `suspect` không bao giờ cho phép UNANSWERABLE một
  mình (đã có luật).

**L3 chuẩn hoá câu hỏi.**
- Triệu chứng: `answer_type` của rewrite làm sai kiểu (8/29 câu sai train); tên hoặc số bị đổi.
- Lệnh: `uv run datamate normalize --split <split>` → `outputs/normalize/`; trong log câu: `pre.changes`,
  `pre.rewrite_skipped`, `pre.norm.normalized` so với `question`.
- Nhìn: rewrite đổi từ nghiệp vụ, mất tên, đổi số; `expect.type` đi từ rewrite thay vì từ luật.
- Quyết định: giữ nguyên tắc "rewrite chỉ đổi cách viết"; kiểu đáp án lấy từ luật (`expect_answer`) trước, rewrite
  sau; thêm từ vào lexicon chỉ khi có trong train (ghi nguồn).

**L4 frame.**
- Triệu chứng: `frame.metric` rỗng ở câu thuộc intent có macro; `residual` dài; `consistency` bắn ở câu đúng.
- Lệnh: thống kê trên log: tỉ lệ `residual = 0`, precision theo `has_metric`, danh sách từ `residual` hay gặp (đếm
  trên train và public, chỉ thêm từ có ở train vào `_EMPTY`/`METRIC_WORDS`).
- Quyết định: từ hay gặp trong `residual` mà rulebook/README định nghĩa → thuộc `vocab`, không phải lỗi; từ nghiệp vụ
  mới (ngành mới) → để router đưa tầng sâu, không sửa cứng.

**L5 solver / router.**
- Triệu chứng: thời gian/câu cao; `length` nhiều; `route.reasons` lệch (một lý do chiếm hết).
- Lệnh: từ log: phân bố `route.tier` và `reasons`, median/p90 `elapsed` theo tầng, tỉ lệ `finish_reason = length`
  theo tầng; `grep -c '"tier": "deep"' logs/*.json`.
- Nhìn: `unfamiliar` chiếm > 50% public (ngưỡng 0.75 quá nhạy với câu lạ của ngành mới); `length` ở tầng sâu (ngân sách
  8192 chưa đủ hoặc model lặp).
- Quyết định: đo `route_far` trên holdout bằng `compare`, không chỉnh theo public; `length` → cổng abstain (P5), và
  attempt kế tiếp tắt thinking (đã có).

**L6 thực thi.**
- Triệu chứng: `had_error` cao (public 178/1000 → 75 abstain); macro table không bind `as_of`.
- Lệnh: gom `attempts[].error` theo lớp lỗi (Binder: cột không có; Catalog: bảng không có; Parser; timeout); kiểm
  `SqlPool.setup` ghi stderr.
- Quyết định: lỗi cột không có ở view canonical → L2 (card thiếu cột trong mô tả) hoặc L5 (model bịa); lỗi ở bảng
  `raw_` → thường là shop mới, để P1/P3.

**L7 kiểm tra kết quả.**
- Triệu chứng: câu đúng bị kiểm tra chặn (false alarm) hoặc kiểm tra im lặng trước lỗi hệ thống.
- Lệnh: `uv run datamate eval-checks <run> ...`: false alarm trên gold train phải = 0 cho mọi kiểm tra cứng; recall trên
  câu sai theo từng kiểm tra.
- Quyết định: kiểm tra có false alarm > 0 trên train → hạ thành đặc trưng mềm; lỗi hệ thống (7.1 điểm 2) **không thể**
  bắt ở L7, đừng thêm kiểm tra độ lớn để cứu nó, để P1.

**L8 quyết định.**
- Triệu chứng: PA âm ở một shop; abstain nhiều ở shop tốt.
- Lệnh: `uv run datamate calibrate <run_train> --out assets/calibrator.json` (báo LOBO: `dropped`, `dropped_right`,
  `bins`, `by_business`); `uv run datamate shop-gate-fit <run_train>` chỉ để xem phân bố; cổng mới đo offline trên log
  trước khi bật (`gate --dry-run`).
- Nhìn: bộ hiệu chỉnh học trên train sạch không bỏ gì (6.2) → không có giá trị cho shop lạ; cổng theo shop phải đọc
  tín hiệu **dữ liệu** (audit) không đọc tín hiệu **lượt chạy** (tỉ lệ abstain) vì lượt chạy đổi theo tập câu.
- Quyết định: ngưỡng cố định trong image; mọi cổng có bảng "train mất / public cứu" như P5 trước khi bật.

**L9 runtime.**
- Triệu chứng: prepare dài, GPU rỗi, một nhóm shop xong sớm.
- Lệnh: `outputs/rented/<run>.run.log` (dòng `prepared in N s`), `part*.log` (tqdm theo shop), log vLLM (số request
  đồng thời, prefix cache hit, token/s); tổng `elapsed` theo tầng từ log.
- Quyết định: phần tuần tự (prepare, một tiến trình chạy lần lượt shop) là nút thắt, không phải GPU; song song theo
  shop và đồng hồ 6 giờ trước, tối ưu model sau.

**L10 đánh giá.**
- Mọi so sánh hai cấu hình: `uv run datamate compare <run_a> <run_b>` (ΔPA, CI bootstrap, bảng chuyển). Không nhận
  một sửa có CI chứa 0 trên holdout chỉ vì public tăng.
- Tổng quát hoá: mutate trên 5 shop holdout (`assets/holdout.txt`: biz_105/110/123/124/128, 16 câu mỗi shop) với các
  mode hiện có và 3 mode mới của P1; báo theo mode: tỉ lệ vai trò khôi phục, contract đúng, cờ audit.
- Tái lập: chạy image 2 lần trên holdout, đếm dòng `submission.jsonl` khác nhau (mục tiêu 0).

---

## 8. Bước tiếp theo sau public_c (2026-11-14, đêm)

Đọc log `outputs/rented/public_c` (gold `ground_truth.jsonl`, **chỉ để đo**) và log train route (3000 câu, có gold,
chạy trước khi có cổng). Quy tắc chọn việc như mục 7: mỗi việc ghi nguồn **T** (train có ca) / **R** (suy từ luật, cơ
chế chung) / **P** (chỉ thấy trên public → bạn quyết, mặc định không làm). Code hiện tại: 388 test xanh; `gates.py`
đã bỏ cổng `length` (public_c vẫn chạy với cổng này, nên số public_c dưới đây là 0.577, bỏ cổng thành 0.590).

### 8.1 Public_c còn gì: 56 sai, 311 abstain

| nhóm | n | gold | ghi chú |
|---|---|---|---|
| abstain ở 4 shop `suspect` (dev_003/011/013/014) | 271 | 186 number, 44 percent, 19 list, 15 string, 3 table, 2 date, 2 unans | chỉ P3 (vá store) lấy lại được; cổng theo kiểu đáp án đã giữ đúng 13 câu (6 unans, 4 date, 3 boolean), 5 sai đều là verdict unanswerable về "thiếu cột nợ/nhóm khách" |
| abstain ở shop clean | 40 | 26 number, 4 string, 3 table, 3 unans, 2 list, 1 date, 1 percent | trần +0.04; không có mẫu chung |
| sai ở dev_012 (sửa xe, audit clean) | 18 | 9 number, 6 string, 2 table, 1 percent | 17/18 đọc `raw_`; 6 string là tên thợ viết tắt trong data (`A.An`, `Đạt`) còn gold là tên đầy đủ; dev_012 ròng +0.04 (20 đúng, 18 sai) |
| sai ở shop còn lại | 38 | | phân loại dưới |

38 câu sai còn lại theo nguyên nhân (đọc SQL và `pre` trong log):

| nguyên nhân | n | shop | nguồn chữa |
|---|---|---|---|
| **`đh`/`dh` → rewrite LLM đổi thành "doanh thu"**: "30 ngày gần nhất được bnhiu đh" → 1 061 009 600 (gold 832) | 6 | dev_004/005/006/007/008 ×2 | lexicon: `đh` **không xuất hiện trong 3000 câu train** (train viết `đơn` 618, `don` 226), nên không được vào `WORDS`; đường đúng là `assets/lexicon_external.json` (nguồn web, qua `accept`). Tiền xử lý được miễn theo quyết định của bạn. Cổng `count_money` không bắt vì câu đã mất từ "đơn" trước khi tới cổng |
| **khách nhóm sỉ/thân thiết → unanswerable**, gold có số | 6 | dev_003 ×2, dev_010 ×4 | `values.py` báo "no column holds it as a value" → cột nhóm khách không được nhận hoặc không nối vào `sales_lines`. Train: 43 câu nhóm khách ở 8 shop, 2 sai (biz_123 bỏ điều kiện nhóm, biz_124 trả table) **vì lý do khác**; không có ca train "cột nhóm bị bỏ sót" → **P**, chỉ chữa khi dựng được ca từ train (mutate đổi header cột nhóm trên biz_108/111/128/129 và đòi role tìm lại qua README) |
| **nợ → unanswerable**, gold có số | 6 | dev_008 ×3, dev_011 ×2, dev_003 | capability `debt` absent; không có ca train tương ứng → **P** |
| giao hàng VLXD (đã chở, ≥ 2 đợt, thùng đã chở) | 3 | dev_004 | train có `SoGiaoHang` (biz_105/117) → kiểm `sql-check` contract trips của hai shop đó trước; nếu train đúng thì là layout khác → P |
| top-k list sai phần tử/thứ tự | 5 | dev_002, dev_004 ×2, dev_006, dev_008 | 1 ca tên khách kèm SĐT trong gold (`Nguyễn Trang Loan 0706783944`), còn lại hoà ở biên hoặc đếm lệch |
| đếm ly size M/L lệch ~1% | 4 | dev_002 | luật topping cùng ô chỉ có ở rulebook public → LLM đọc rulebook, không chữa code |
| khác: trung bình đơn → tổng; nợ quá 7 ngày number → list; lô gần hết hạn; % sau tăng giá; dthu th8; dthu COD ×10; tên kèm SĐT; bảng nợ | 8 | dev_006 ×4, dev_004, dev_008 ×2, dev_010 | lẻ tẻ, không mẫu chung |

### 8.2 Train route còn gì: 29 sai, 30 abstain (trước cổng)

Phát lại cổng trên log train (`datamate gate <run_train> --gold data/train/answers.jsonl`): `count_money` 4 sai → 0,
`items_len` 4 sai → đúng, `table_words` 5 sai → 0, `unans_in_sql` 3 abstain → đúng; train 0.9707 → 0.9773. **16 sai còn
lại** là ca train thật, có gold, sửa được không chạm public:

| nhóm | n | ca | cơ chế |
|---|---|---|---|
| tồn kho nhà thuốc đọc `raw_baocaotonkho."Tồn cuối kỳ"` với mã hàng **đoán** (`SP0001` cho hai sản phẩm khác nhau), bỏ qua fact stock | 4 | biz_120 | lint: literal mã hàng trong SQL không phải mã mà frame đã resolve cho entity → sửa một lượt với gợi ý "dùng macro stock"; đo trên train trước khi bật (kỳ vọng 4/4) |
| % đơn ca chiều so ca sáng lệch nhỏ (−18.52 vs −18.17; −20.72 vs −20.88; −55.17 vs 65.0) | 3 | biz_105/106/117 | kiểm `SHIFT_MACROS` với giờ ca đọc từ rulebook và đơn huỷ; 1 giờ |
| frame có giá trị thực thể (`customer_group = 'sỉ'`, `MaNhanVien`) mà SQL không dùng | 2 | biz_123 ×2 | lint "entity của frame không xuất hiện trong SQL" → sửa một lượt; R + T |
| còn lại: khách mua nhiều đơn nhất, `stock_split`, giá một viên, dthu hôm qua, đếm YTD 27071/27707 | 7 | biz_121 ×2, biz_123, biz_128, biz_112, ... | xem từng ca; không mẫu |

30 abstain: 14 gold unanswerable (đúng là nên abstain), 16 trả lời được (5 percent, 5 number, 5 list, 1 string): trần +0.005.

### 8.3 Thứ tự làm, không chạm public

| thứ tự | việc | nguồn | giờ | kỳ vọng (public chỉ để báo) | điều kiện nhận |
|---|---|---|---|---|---|
| 1 | **Docker**: `docker/` đã có Dockerfile, `predict.sh`, README, `build.sh`; chưa có bằng chứng image đã build và chạy. Build, chạy 2 lần trên 5 shop holdout với `--network none`, so `submission.jsonl` | bắt buộc | 4–5 | không có image = không có điểm | 2 lần ra cùng số dòng và cùng đáp án; log có `answer`, `elapsed_ms`, `thinking`, `code`, `output`; `/data` chỉ đọc |
| 2 | `đh`, `dh` → "đơn hàng" trong `lexicon_external.json` (nguồn web, qua `accept`; không vào `WORDS` vì train không có) + test | tiền xử lý (miễn) | 0.5 | public +6 sai → đúng (≈ +0.012); private: "đh" là viết tắt phổ biến | 3000 câu train chuẩn hoá không đổi (train không có `đh`); test `bnhiu đh` → `bao nhiêu đơn hàng` |
| 3 | lint mã hàng literal ≠ entity đã resolve (biz_120) + lint entity của frame không có trong SQL (biz_123) → sửa một lượt | T | 2 | train +4..+6; dev_012 đọc `raw_` 17/18 sai có thể bớt | `datamate gate --dry-run` trên log train: không câu đúng nào đổi; `sql-check` 139/139 |
| 4 | % ca chiều (3 ca train) | T | 1 | train +3 | 3 câu đúng, `semantic-check` không lùi |
| 5 | **P3** vá store cho shop `suspect` (mục 7.3), chỉ sau khi image đã nộp được | R, kiểm bằng mutate | 6–8 | 271 abstain là toàn bộ phần còn lấy được trên public (trần +0.27, thực tế có lẽ chỉ dev_003); đường lùi là abstain nên không âm | mutate `status_in_notes`, `amount_before_discount`: contract ≥ 95%; train no-op |

Không làm trước hạn: đổi model, sửa prompt/router, `route_far`.

### 8.4 Quyết định của bạn (P: chỉ thấy trên public)

- **Ngành mới chưa bị audit cờ (kiểu dev_012).** Cơ chế R có sẵn: rulebook có section ngành mà mọi dòng đều là
  `rule_delta` và README shop nói đúng ngành đó → mức `unfamiliar_industry`. Nhưng *dùng mức đó để abstain kiểu nào* thì
  chỉ public cho số (string 0/6, table 0/2, number 15/9): ngưỡng là P. Nếu không làm, dev_012-kiểu ở private cho ≈ 0 ròng.
- **Shop `suspect` + verdict unanswerable về "thiếu cột"**: 6 đúng / 5 sai trên public → abstain thêm được −1, không đáng.
- **Nhóm khách và nợ không được nhận ở dev_003/008/010/011** (12 câu sai): không có ca train. Có thể dựng ca từ train
  bằng mutate (đổi header cột nhóm/nợ, giữ README) và chữa theo mô tả README như `role_gap` (mục 7, hàng 7), nhưng động
  lực là public → bạn quyết.
- Ghi chú tuân thủ: prompt của `preprocess/lexgen.py` (công cụ dev) liệt kê "cửa hàng điện thoại, tiệm sửa xe" trong câu
  mô tả bối cảnh; hai ngành này chỉ có ở public. Các mục sinh ra vẫn qua `accept` từ seed train, nhưng nên bỏ hai chữ đó
  khỏi prompt nếu chạy lại.

### 8.5 Đã làm đêm 14/11 và còn lại

Docker image #1 đã build không cần daemon trên server (`docker/assemble.py`): 43 layer, `.tar` 47 241 054 208 byte,
MD5 `2992a15d5d4f8c29d75a7f885fa97de5`. Code của image là code public_c, đã bỏ cổng `length`, **chưa có** các sửa dưới
đây. Image chưa chạy lần nào, nên việc 1 ở mục 8.3 (chạy 2 lần trên holdout) vẫn chưa đạt.

| việc ở 8.3 | trạng thái | căn cứ |
|---|---|---|
| 2. `đh`/`dh` → "đơn hàng" | xong, trong `lexicon_external.json` | tiền xử lý (miễn); 0 câu train chứa chữ này |
| 3. lint mã hàng đoán + giá trị của frame không có trong SQL | xong, trong `lint.frame_advice`: mã hàng đoán trên bảng tồn thô (biz_120 ×4); nhóm khách của frame bị bỏ (biz_123) | T |
| 3b. lint mã nhân viên đoán (biz_123) | **bỏ**: 1 ca train; đo trên public thì hỏi lại 3 câu đúng | T yếu |
| 4. % ca chiều | xong: `shift_orders` khi hỏi "số đơn" (biz_105/106), thiếu `pct_change` (biz_117) | T |
| thêm | `count(*)` trên `sales_lines` khi đếm đơn (biz_121); đơn vị của `stock_split` khác đơn vị được hỏi (biz_121); ngày trong SQL không trùng kỳ của frame (biz_128); bảng bỏ cột mã đứng trước cột tên (biz_127) | T |
| 5. P3 | chưa làm | |

Đo trên lượt chạy đầu của log train route: lint bắn 14 câu sai, 10 câu cuối cùng vẫn đúng. Trong 10 câu đó, lời nhắc vẫn
đúng nghĩa: ví dụ `count(*)` đếm dòng nhưng tình cờ xếp hạng đúng. Lượt `train_check` (106 câu: 46 câu dính lint hoặc
trả lời dạng bảng, 60 câu đối chứng) và `public_d` đang chạy để đo bằng model thật. Chỉ giữ một lint khi kết quả train
ủng hộ nó.

### 8.6 Kết quả public_d, train_check và thử nghiệm P3 (2026-11-14, khuya)

**Hai lượt chạy** (code `291d1bf`):

| lượt | PA | đúng / sai / abstain | ghi chú |
|---|---|---|---|
| public_d (gold `ground_truth.jsonl`, chỉ để đo) | 0.595 | 649 / 54 / 297 | public_c 0.577; phần tăng chủ yếu nhờ `đh` |
| train_check (106 câu train) | 0.9528 (trước cổng 0.9151) | 101 / 0 / 5 | so với route-train trên cùng 106 câu (88 / 18 / 0): W→R 13, W→A 5, **R→W 0** |

Các lint của mục 8.5 giữ được trên train. Đây là số in-sample, vì lint được viết từ chính các lỗi này.

**Abstain của public_d theo lớp** (297 câu):
- cổng `shop_suspect` 212: nếu không chặn thì 181 câu sai, 31 câu đúng;
- lỗi SQL 56;
- reply bị cắt `length` 15: với `--repair 1`, lượt thử lại không thinking trong `_samples` không còn lượt nào;
- còn lại 15.

Cổng không phải nguyên nhân. Nó chặn đúng những câu đáng lẽ đã sai.

**Nguyên nhân ở ngành mới: store, không phải model.**
- Cách đo: phát lại SQL trong log public_d trên store dựng lại (`debug/p3_slot/replay.py`; bản đối chứng tái tạo
  230/230 đáp án).
- Bốn shop `suspect` trước cổng có 40 đúng / 189 sai. SQL của model phần lớn đã đúng; store bỏ sót quy ước riêng của
  shop.
- Ví dụ dev_013: chỉ cần bỏ các đơn có `cancel` hoặc `Huỷ` trong `Ghi chú`, doanh thu tháng 8/2025 ra đúng 466 616 500,
  khớp gold. Store hiện tại cho 486 110 000.
- Vì vậy đổi model trả lời câu hỏi không chữa được: model nào cũng truy vấn đúng store sai đó.

**P3 với Qwen3.5-9B, hai cách** (code nháp trong `debug/p3_slot/`, gitignored; không có gì trong `src/`):

| cách | kết quả |
|---|---|
| Model viết lại cả `sales_all` thành một SELECT 25–45 cột (`p3_probe.py`, kèm một vòng tự kiểm) | **hỏng**: thinking bị cắt ở 12k và 20k token. Không thinking thì SQL chạy được nhưng sai: đảo `is_goods`, sót `Huỷ`, gọi bảng không tồn tại. dev_003 và dev_014 không ra SELECT nào chạy được |
| Model điền một ô có kiểu, code áp tất định (`slot_probe.py` + `patches/generic_slot.py`) | **được**: ô là `{status_column, status_map → 4 trạng thái của rulebook, order_column}`; prompt gồm README, các dòng rulebook về trạng thái và giá trị các cột chưa có role; khoảng 100 s/shop; code áp trạng thái xấu nhất cho cả đơn |

Kết quả cách điền ô (đáp án tiềm ẩn, trước cổng):

| shop | trước (đúng / sai) | sau | W→R | R→W | model điền |
|---|---|---|---|---|---|
| dev_013 | 5 / 48 | 34 / 19 | 30 | 1 | `Ghi chú`: cancel, Huỷ → huỷ |
| dev_003 | 13 / 81 | 25 / 69 | 13 | 1 | `Trạng thái giao` (4 giá trị) + `Đơn số` làm mã đơn |
| dev_011 | 14 / 22 | 20 / 16 | 6 | 0 | ghi chú trả hàng; audit bỏ sót vì 0,1% < ngưỡng 0,5% |
| dev_014 | 8 / 38 | 8 / 38 | 0 | 0 | không có lỗi trạng thái |

- `order_column` model chọn không đáng tin: nó chọn `STT`, là số dòng. Chỉ dùng khi store chưa có `order_id`.
- Sau khi sửa, cờ của dev_013 hết nên cổng mở: 34/53 câu đúng tiềm ẩn, khoảng +15 câu public.
- dev_003 (`discount_unapplied`) và dev_011 (`fill_gap`) vẫn bị chặn, và chặn là đúng: tỉ lệ đúng 27% và 56%.
- Còn 142/229 câu sai, mỗi nhóm là một quy ước khác, cần một ô điền riêng theo cùng khuôn:
  - sheet viết tắt `Ghi tắt`: `name_gap` 0,80–0,90 ở dev_011/014;
  - chiết khấu dòng (dev_003);
  - thuộc tính nhóm hàng và size (dev_013, chỉ khớp 58–90%);
  - dòng ship, voucher, điểm.

**Bạn quyết.**
- Tuân thủ của ô trạng thái:
  - rulebook train có dòng 109/110 (4 trạng thái; trạng thái áp cho mọi dòng của đơn);
  - dòng 143 ("sổ không có cột trạng thái → trạng thái ở Ghi chú") chỉ có ở public; model đọc nó lúc chạy, code không
    ghi cứng;
  - ý tưởng ô điền đến từ phân tích public.
- Có tích hợp hay không. Nếu có:
  - làm một bước prepare, chỉ chạy khi mọi trạng thái chuẩn là `hoàn thành`;
  - kiểm trên train: không câu đúng nào đổi;
  - rồi chạy live public.
- Gemma 4 12B chưa thử. Nếu thử thì thử trên đúng bài điền ô, và phải dừng Qwen.

## 9. Lớp ngữ nghĩa shop bằng ô điền có kiểu (quyết định 2026-11-15)

Bạn chọn **cách 2**: model điền ô có kiểu, code áp tất định. Hai mục tiêu đi cùng: (a) không để một output model
làm sai hàng loạt; (b) tổng quát cho ngành lạ và luật mới, kể cả luật chỉ có ở rulebook của split lúc chạy (ví dụ
"sổ không có cột trạng thái thì trạng thái ở Ghi chú"). Mục này thay P3 ở 7.3 và là kế hoạch cho phiên tiếp theo.

### 9.1 Hiện trạng đã kiểm (store `i21_s14` của public, 15/11)

**`sales_all` đã giữ nguyên mọi cột chưa có role** của bảng dòng và bảng đầu đơn (`canon._extra_cols`, cột được chép
không đổi, tên gốc). Bằng chứng trên store:

| shop | cột chưa role đang nằm trong `sales_all` |
|---|---|
| dev_003 | `Trạng thái giao`, `Tiền giảm`, `Giảm (đ)`, `Tiền hàng trước giảm`, `Phí ship`, `Công nợ`, `Nguồn`, `Nhóm hàng`, `Giá vốn lúc bán` |
| dev_011 / dev_014 | `Ghi chú`, `Ghi tắt`, `Loại`, `Bớt`, `Thu máy cũ`, `Khách trả`, `IMEI` |
| dev_013 | `Ghi chú`, `Kênh`, `Nhóm`, `Hãng`, `Số / Size`, `Tên gọi khác`, `Quy đổi` |

Vậy "sales lines chứa toàn bộ thông tin" phần lớn đã có. Cái **thiếu là nghĩa**: `status`, `counted`, `amount`,
`is_goods`, `qty_base`, `product_name` chuẩn do code tính theo role regex, không nhìn các cột trên. Model SQL gọi macro
trên `counted` nên sai hàng loạt dù dữ liệu nằm ngay cạnh. Ba lỗ hổng thật sự về dữ liệu:

1. `canon._JUNK` (canon.py:63) bỏ cột theo tên: `thuoc tinh`, `bao hanh`, `mo ta`, `stt`, `so thu tu`. Với ngành lạ
   đó là dữ liệu (bảo hành ở điện thoại, thuộc tính ở mẹ và bé).
2. Bảng không nối được vào dòng bán chỉ tồn tại dạng `raw_*`, không thành fact: dev_014 `sono_so_no`, `sono_tra_gop`,
   `sono_bao_hanh`, `thumaycu`; dev_013 `quanly_khuyenmai`, điểm khách. Model thấy tên bảng, không thấy nghĩa.
3. Shop không dựng được `sales_all`: dev_012 (`raw_sosuaxe` có `Tiền`, `Làm gì`, `Thợ` nhưng không cột nào khớp regex
   `line_amount`). Mọi câu đọc `raw_` và 17/18 câu sai.

Model hiện can thiệp ở hai điểm hẹp: `agent/statuses.py` (giá trị lạ **trong cột đã có role status**) và
`agent/columns.py` (cột chưa role **khi thiếu role bán hàng**). Cả hai chỉ chạy khi rule tự biết mình thiếu. dev_013
có cột status "đủ" (mặc định `hoàn thành`, canon.py:349) nên không ai hỏi về `Ghi chú`. Đó là cơ chế hỏng chung:
**rule không biết mình đang sai**, nên điểm kích hoạt phải là "có khoảng trống" chứ không phải "rule báo thiếu".

### 9.2 Khuôn chung của một ô

Mỗi ô (`Slot`) gồm 5 phần, tất cả tất định trừ phần điền:

| phần | nội dung |
|---|---|
| `trigger(biz, sales, audit)` | điều kiện có khoảng trống, tính từ dữ liệu, không cần cờ audit. Trên 30 shop train phải không bắn, hoặc bắn mà áp là no-op (đo bằng `datamate slots --split train --dry-run`) |
| `prompt(biz, sales, rulebook)` | README của shop; các dòng rulebook **của split lúc chạy** lọc theo từ khoá của ô; card các cột chưa role kèm giá trị (≤ 30 giá trị distinct liệt kê hết, còn lại 4 ví dụ) như `slot_probe.py`; 3 dòng mẫu |
| `schema` | JSON kiểu hẹp: tên cột phải có thật, giá trị phải có thật trong dữ liệu, nhãn thuộc enum của rulebook. Code từ chối mọi thứ ngoài danh sách |
| `apply(sales, tables, slot)` | hàm thuần, ghi cột canonical, giữ cột gốc (`*_raw`), ghi `slot_<name>` vào store để log |
| `check(before, after)` | bất biến vật lý không cần gold (xem từng ô), chạy lại audit: cờ cũ tắt, không cờ mới, số dòng không đổi |

Đường lùi: ô không điền được, JSON hỏng, hoặc rớt `check` → giữ store cũ, audit giữ `suspect`, cổng chặn như hiện nay.
Không bao giờ âm hơn public_d.

Chạy trong `prepare`, mỗi shop một lần, các ô theo thứ tự 9.3, ô sau thấy kết quả ô trước. Cache
`cache/ingest/<split>/<biz>.slots.json` khoá theo (ô, băm README, băm rulebook, model, phiên bản prompt). Thinking bật,
greedy, seed 0; bị cắt `length` thì thử lại không thinking (đã có trong probe). Khoảng 100 s mỗi ô mỗi shop, song song
theo shop, nằm trong ngân sách.

**Hạn chế sai hàng loạt**, bốn tầng:
1. Một ô là một quyết định nhỏ (cột nào, giá trị nào ứng nhãn nào). Không SQL tự do, không viết lại `sales_all`
   (đã đo là hỏng, 8.6).
2. `check` vật lý per ô; rớt là bỏ ô, không bỏ shop.
3. **Mutate train có gold** (`eval/mutate.py`, đã có `propose_roles`): sinh biến thể của shop train đúng theo lỗi mà ô
   chữa, chạy contract `sql-check` trên store mutate có áp ô; nhận khi ≥ 95% và shop train gốc không đổi (no-op). Đây
   là phép đo gold duy nhất không chạm public.
4. `train_check` (106 id) sau mỗi ô: R→W phải là 0.

**Tuân thủ.** Cơ chế là chung: model đọc README và rulebook của split lúc chạy; code không ghi layout, từ, hay ngưỡng
học từ public. Ý tưởng các ô 2, 4, 6, 7 đến từ phân tích public (P), bạn đã quyết làm (15/11); điều kiện nhận chỉ bằng
train và mutate. Bỏ "ghi chú" khỏi regex lọc dòng rulebook của ô status (dòng 143 public vẫn khớp "trạng thái").

### 9.3 Danh sách ô, theo thứ tự làm

| # | ô | trigger | schema (model điền) | apply | check | gold |
|---|---|---|---|---|---|---|
| 1 | `status_text` | mọi `status` = hoàn thành và có cột text chưa role | `{status_column, status_map: giá trị → 4 trạng thái, order_column?}` | như `debug/p3_slot/patches/generic_slot.py`: xấu nhất theo đơn; `order_column` chỉ khi store không có `order_id` | audit `status_in_text` tắt; tỉ lệ đơn huỷ sau áp trong (0, 30%] | rulebook train dòng 109/110; mutate `status_in_notes` (chuyển cột trạng thái của biz train vào Ghi chú, xoá cột) |
| 2 | `name_map` | `name_gap` ≥ 0.3 và có bảng ≤ 4 cột với hai cột tên, hoặc README nói viết tắt | `{table, short_col, full_col}` | `product_name` = map(short → full), giữ `product_name_raw`; `dim_product` dựng lại | `name_gap` giảm; không tên tắt nào ứng 2 tên đầy đủ; số dòng không đổi | mutate `abbrev_names` trên biz train có danh mục (viết tắt tên trong sổ bán, thêm sheet 2 cột) |
| 3 | `line_amount` | cột giảm dòng tồn tại và amount = qty × giá (audit `discount_unapplied`) | `{discount_col, kind: amount∣percent, level: line∣order}` | `line`: amount = qty × giá − giảm; `order`: giữ (rulebook: không trừ giảm đơn) | amount ≤ qty × giá trên ≥ 95% dòng có giảm; audit tắt | mutate `amount_before_discount` |
| 4 | `non_goods` | cột tên hàng hoặc loại hàng có giá trị kiểu phí ship, voucher, điểm, thu máy cũ, trả góp | `{column, values: [{value, kind: fee∣discount∣tradein∣points∣installment}]}` | `is_goods` = False cho các dòng đó; dấu tiền theo rulebook của split (ship không vào doanh thu; thu máy cũ là trừ tiền khách trả, không trừ doanh thu) | doanh thu tổng đổi < 20%; các dòng bị loại không có `qty` hoặc tên khớp đúng giá trị model nêu | `drop_fee_items` đã có ca train phí phục vụ; mutate thêm dòng ship vào biz train |
| 5 | `attributes` | cột text chưa role, ≤ 50 giá trị distinct, phủ ≥ 50% dòng hàng | `{column → role: product_group∣brand∣size∣channel∣staff∣pay_method}` | chỉ gắn nhãn role vào card và `dim_product` (không đổi số) | giá trị khớp mẫu của role | train có nhóm hàng và kênh (biz_108/123/124): no-op vì đã có role |
| 6 | `side_tables` | `raw_*` chưa thành fact, có cột ngày và tiền | `{table → kind: debt∣payment∣installment∣tradein∣warranty∣promo∣expense, columns: {...}}` | `debt`/`payment`/`expense`: dựng fact bằng `facts.py` hiện có; kind lạ: gắn mô tả có kiểu vào card để SQL đọc đúng cột | contract debt/expense trên train không đổi; fact dựng được có ≥ 1 dòng | mutate đổi header sổ nợ của biz_105/117 |
| 7 | `sales_from_log` | không dựng được `sales_all`, có bảng có ngày + tiền + tên việc/hàng | `{table, date_col, amount_col, item_col, staff_col?, customer_col?}` | qua `propose_roles` (khuôn đã có) rồi `build_sales` | số dòng > 0, tổng tiền > 0, mọi ngày trong khoảng README | mutate đổi header `ban_hang.csv` biz_101 thành từ lạ |

Kỳ vọng public (chỉ để báo, từ 8.6): ô 1 mở dev_013 (+15), ô 2–4 mở dần dev_003/011/014 (trần 229 câu), ô 7 cho
dev_012 (18 sai hiện nay). Ngành lạ ở private: ô nào bắn thì có cơ hội; không bắn thì như hôm nay.

### 9.4 Thay đổi code

- `src/datamate/slots/`: `base.py` (`Slot` với 5 phần trên, `run_slots(biz, sales, tables, rulebook, generate, log)`),
  một file mỗi ô, `cache.py`. Ô 1 chuyển từ `debug/p3_slot/` (prompt, parse, apply đã chạy được).
- `canon.py`: `_JUNK` chỉ áp cho bảng danh mục; bảng dòng giữ mọi cột trừ `hinh anh`, `url`, `barcode`. Kiểm
  `sql-check` 139/139 và card không phình quá 10%.
- `sql/store.py`: `build_sales` → `run_slots` → `build_facts` → ghi; bảng `slots(name, json)`; card thêm đoạn
  "Quy ước của shop đã áp: ..."; bump `STORE_VERSION`.
- `audit.py`: thêm feature `unroled_text_cols`, `raw_without_fact`, `no_sales`; audit chạy lại sau slots, cổng
  `gates.py` giữ nguyên và đọc audit mới.
- `cli.py`: `datamate slots --split X [--biz B] [--dry-run]` in trigger, JSON, check của từng ô; `sql-check` và
  `audit` chạy trên store có slots.
- `eval/mutate.py`: `status_in_notes`, `abbrev_names`, `amount_before_discount`, `rename_debt_headers`,
  `rename_sales_headers`.
- Log mỗi câu: thêm `slots` (JSON đã áp của shop) cạnh `audit`.
- Test: mỗi ô một fixture nhỏ (apply + check); một test trigger trên cache ingest train: không shop nào đổi
  `sales_all`.

### 9.5 Thứ tự và điều kiện nhận

| bước | việc | nhận khi |
|---|---|---|
| 1 | khung `slots/` + ô 1 từ probe + mutate `status_in_notes` | train dry-run no-op; mutate ≥ 95%; `train_check` R→W 0; `sql-check` 139/139 |
| 2 | `_JUNK` + card "quy ước đã áp" + log | `sql-check` không đổi; 388 test xanh |
| 3 | ô 2 + mutate `abbrev_names` | như bước 1 |
| 4 | ô 3 + mutate `amount_before_discount` | như bước 1 |
| 5 | ô 4, ô 5 | như bước 1; ô 5 train no-op |
| 6 | ô 6, ô 7 | contract debt train không đổi; mutate header |
| 7 | chạy live public theo shop; image #2 | đo PA theo shop, so với public_d; không shop quen nào giảm |

Mỗi bước là một commit riêng, bật được tắt được bằng `--slots none|1|1,2,...` để đo tách.

### 9.6 Đã làm 15/11 (chưa commit)

Bạn duyệt trong phiên: ô 0 `order_key` (thêm, không có trong 7 ô ở trên), ô 1–3, hoãn ô 4 (không có ca thật ở train
lẫn public), giữ cổng `fill_gap`, retry khi lượt sửa bị cắt `length`, bỏ "điện thoại, sửa xe" khỏi prompt `lexgen.py`.

**Code.**
- `src/datamate/slots/apply.py`: phần áp tất định, hàm thuần.
  - `order_key` thành role `order_id` khi ingest (`load_business` gộp vào `extra_roles`), nên fill §8 và bỏ dòng trùng
    chạy trên nó;
  - `name_map` đổi tên tắt trong bảng dòng trước khi `build_sales` nối danh mục;
  - `status_text` và `line_amount` sửa dòng trước khi tính `order_ok`/`counted`.
- `src/datamate/slots/base.py`: mỗi ô có trigger, prompt, parse, check.
  - `run_slots` chạy theo thứ tự; `resolve_slots` lưu `cache/ingest/<split>/<biz>.slots.json`.
  - Khoá gồm README, rulebook, model, ô bật, nội dung prompt. Reply của model được dùng lại khi prompt không đổi.
- `canon.build_sales(tables, roles, slots)` và `sql/store.build_store(..., slots)`; `STORE_VERSION` 15; tag store gồm
  file slots. Card thêm dòng "Shop conventions applied ...".
- `_JUNK` chỉ áp cho bảng danh mục. Bảng dòng và bảng đầu đơn chỉ bỏ `hình ảnh`, `barcode`, `mã vạch`, `url`.
- Pipeline gọi `resolve_slots` sau `resolve_statuses`, dùng `think` (thinking, cắt `length` thì hỏi lại không thinking).
  Cờ `--slots all|none|tên,...`, mặc định `all`. Có `slots.json` theo run và trường `slots` trong log mỗi câu.
- `datamate slots --split X [--biz] [--dry-run] [--mutate all]`.
- Mutate mới: `status_in_notes`, `rename_order_key` (đổi header và bỏ dòng README của cột), `amount_before_discount`.
- `_samples`: khi lượt sửa bị cắt trống thì thêm đúng một lượt không thinking (train route có 8 ca).
- `kaggle/run_kaggle.py`: stage `train_check` (`assets/train_check.txt`, 106 id); chép `*.slots.json` vào output.

**Điều kiện nhận đã đo.**

| điều kiện | kết quả |
|---|---|
| train no-op (`datamate slots --split train --dry-run`) | 30/30 shop không ô nào bắn: các ô không đổi được câu train nào |
| public: trigger | chỉ bắn ở dev_003, dev_011, dev_013, dev_014 (4 shop `suspect`); 10 shop clean không bắn |
| `sql-check` train sau `_JUNK` | giống hệt bản đối chứng: order_count 147/162, revenue 273/291, cancel_count 100/100, avg_order_value 167/167, cancel_rate 139/139 |
| test | 400 xanh (9 test mới) |

Qwen3.5-9B local (Ollama, không thinking) điền 4 shop public, mọi ô qua check:
- dev_003: `order_key` = `Đơn số`; `status_text` = `Trạng thái giao` (4 giá trị);
- dev_011: `status_text` = `Ghi chú` (3 cách ghi trả hàng); `name_map` = sheet `Ghi tắt`;
- dev_013: `status_text` = `Ghi chú` (cancel, Huỷ, đang giao);
- dev_014: `order_key` = `STT`; `status_text` = `Ghi chú`; `name_map` = `Ghi tắt`.

Sửa sau lần chạy đầu, đều là sửa chung: khi model bỏ trống tên cột hoặc bảng mà giá trị nó liệt kê chỉ khớp một ứng
viên thì code suy ra; khuôn JSON của `name_map` nói rõ cần tên cột; ứng viên `order_key` không gồm cột ngày; bảng viết
tắt phải có một dòng mỗi tên tắt và một cột tên dài hơn (loại `README.QuyDoi` của biz_101/107).

**Phát lại SQL của public_d trên store có ô** (đáp án tiềm ẩn, trước cổng; public chỉ để đo):

| shop | trước (đúng / sai) | sau | W→R | R→W | audit sau |
|---|---|---|---|---|---|
| dev_013 | 7 / 48 | 34 / 19 | 30 | 1 | clean |
| dev_003 | 15 / 85 | 65 / 9 | 53 | 0 | clean (`order_key` làm cờ `discount_unapplied` giả tự tắt: `Tiền giảm` là giảm của đơn) |
| dev_011 | 14 / 25 | 35 / 1 | 21 | 0 | suspect (`fill_gap`, cột `Thu máy cũ`): cổng giữ theo quyết định của bạn |
| dev_014 | 10 / 38 | 27 / 14 | 19 | 0 | suspect (`fill_gap`, sổ nợ) |

25 câu thành lỗi SQL khi phát lại vì SQL cũ gọi cột `"Đơn số"`, giờ đã là `order_id`. Chạy live thì model thấy cột mới.
Chạy đầu-cuối local 6 câu (dev_003 ×3, dev_013 ×3, không thinking): 6/6 đúng.

**Mutate train có gold** (`datamate slots --split train --mutate ...`, Qwen3.5-9B Ollama không thinking; contract của
`eval/semantic.py`; baseline không mutate 826/859):

| ô | mode | contract khớp | ghi chú |
|---|---|---|---|
| 0 `order_key` | `rename_order_key` (đổi header mã đơn, bỏ dòng README) | **826** (100% baseline) | cần nhận mã đơn chỉ ghi ở dòng đầu (§8, kiểu Sapo): lượt đầu 750 |
| 1 `status_text` | `status_in_notes` (bỏ cột trạng thái, ghi vào Ghi chú) | **802** (97.1%) | phần lệch ở biz_116; lượt đầu 738 (model bỏ sót giá trị trả hàng) |
| 2 `name_map` | `abbrev_names` (4/5 dòng ghi tên tắt, thêm sheet 2 cột) | **826**; tên hàng đúng 2 309 211 / 2 309 211 dòng | không ô: 21% dòng đúng tên (biz_101/105) |
| 3 `line_amount` | `amount_before_discount` | **826** | không ô: 718 |

Sửa ô 1 sau mutate, đều chung và đều giữ train no-op:
- giá trị model bỏ sót mà bộ chuẩn hoá trạng thái của train (`normalize_status`) nhận ra thì code điền;
- model trả null mà đúng một cột có mọi giá trị là trạng thái train biết, ít nhất một giá trị không phải hoàn thành, thì
  dùng cột đó (bạn duyệt). dev_011 có `khách mang trả` nên cách lùi này không bắn, phần đó vẫn do model.
- câu prompt "map EVERY value" làm Qwen không thinking trả null ở 10 shop (596/859), nên đã bỏ.

Kaggle (tài khoản 1 public chạy code lúc đẩy, chưa có các sửa sau mutate; tài khoản 2 train_check chờ máy):
`hydrochii/datamate-slots-public`, `hydrochiii/datamate-slots-train-check`. Sau commit, đẩy thêm
`hydrochii/datamate-slots-public2` với code mới.
