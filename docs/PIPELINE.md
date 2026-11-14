# Pipeline DataMate: từ câu hỏi đến submission (2026-11-14)

Tài liệu này mô tả pipeline đúng như code hiện tại trên nhánh `text2sql`, gồm cả các sửa đổi chưa commit (P4, P5, ShopAudit,
order_goods, P1b, ingest 23). Cấu hình được mô tả là cấu hình nộp bài trong [docker/predict.sh](../docker/predict.sh). Mục 8
liệt kê những phần có trong code nhưng không chạy ở cấu hình này.

---

## 0. Sơ đồ tổng

```
predict.sh
 ├─ khởi động vLLM (Qwen3.5-9B, TP=2, bf16)
 ├─ lượt prepare-only: chuẩn bị mọi shop một lần (cache)
 └─ 12 tiến trình `datamate run`, mỗi tiến trình một nhóm shop, cùng ghi vào /result

datamate run  (src/datamate/agent/pipeline.py: run)
 A. Chuẩn bị theo split (một lần)
    rulebook ─► rule_params, rule_gate, rule_delta ─► system prompt SQL
    train questions + gold answers ─► ExampleBank
 B. Chuẩn bị theo shop (một lần mỗi shop)
    file thô ─► ingest ─► (LLM: vai trò cột, trạng thái đơn) ─► sales_lines ─► trust, facts, capabilities, audit
             ─► DuckDB store ─► SqlPool + schema card + EntityIndex
 C. Mỗi câu hỏi  (solve_question, 10 câu song song mỗi tiến trình)
    C1 hạn giờ
    C2 tiền xử lý: luật ─► (LLM rewrite nếu còn từ chưa giải thích)
    C3 code đọc câu hỏi: kiểu đáp án, giá trị, dòng rulebook, kỳ thời gian, ví dụ, frame
    C4 dựng prompt
    C5 router: tầng normal (thinking off) hay deep (thinking on)
    C6 LLM viết SQL ─► chạy trên DuckDB ─► kiểm tra ─► sửa 1 lần
    C7 vote
    C8 leo thang lên deep nếu câu trả lời đầu có dấu hiệu sai
    C9 judge (agent thứ hai) cho UNANSWERABLE / câu trả lời có rào đón
    C10 cổng quyết định (gates) ─► abstain hoặc sửa dạng
    C11 finalize ─► submission.jsonl + logs/<id>.json
```

---

## 1. Điểm vào và cấu hình nộp bài

[docker/predict.sh](../docker/predict.sh) không nhận tham số. Các bước:

1. Ghi `T0`, đặt `DEADLINE = T0 + 6h − 25 phút` và `DEEP_MARGIN = 3600 s`.
2. Liên kết `/data` thành `<WORK>/root/test`, vì `/data` chỉ đọc được, còn cache và DuckDB phải ghi vào `/tmp/datamate`.
3. Khởi động vLLM với các tham số `--tensor-parallel-size 2 --dtype bfloat16 --max-model-len 32768
   --gpu-memory-utilization 0.90 --max-num-seqs 128 --max-num-batched-tokens 16384 --enable-prefix-caching
   --reasoning-parser qwen3 --language-model-only --seed 0`.
4. Chạy `datamate run --prepare-only` một lần: ingest, gọi LLM cho vai trò cột và trạng thái, dựng store, mọi thứ được cache.
5. Chia shop thành 12 nhóm cân theo số câu hỏi. Mỗi nhóm chạy `datamate run --biz <các shop> --concurrency 10`, tất cả ghi vào
   `/result/submission.jsonl` và `/result/logs/`.
6. Nếu vLLM chết, khởi động lại (tối đa 4 lần). Lượt chạy sau bỏ qua các câu đã có trong submission (`resume`).

Cờ của `datamate run` trong lượt nộp:

| cờ | ý nghĩa |
|---|---|
| `--engine sql` | mô hình viết một câu SELECT DuckDB |
| `--thinking route` | mặc định tắt thinking; câu nào được router chọn thì bật |
| `--max-tokens 2048` | ngân sách token của tầng normal; tầng deep dùng `--think-max-tokens` (mặc định 8192) |
| `--catalog delta` | dùng macro viết tay; macro của luật bị rulebook đổi thì tắt; không biên dịch macro mới (`compile_delta` off, P4) |
| `--judge unans` | agent thứ hai kiểm tra phán quyết UNANSWERABLE và câu trả lời có rào đón |
| `--context rich` | card có khóa nối bảng, fact còn thiếu, capabilities; prompt có các dòng rulebook liên quan; truy vấn mở đầu bằng 3 dòng kế hoạch |
| `--frame` | in khung câu hỏi (frame) vào prompt |
| `--rewrite residual` | chỉ gọi LLM rewrite khi bản đã làm sạch bằng luật còn từ chưa giải thích được |
| `--exec-workers 4` | 4 cursor DuckDB mỗi shop |
| `--deadline`, `--deep-margin` | hạn giờ tuyệt đối (epoch) và lề tắt tầng deep |
| (mặc định) `--samples 1 --repair 1` | 1 mẫu greedy (temperature 0), tối đa 1 lượt sửa |
| (mặc định) gates bật | cổng quyết định ở bước C10 |

---

## 2. Giai đoạn A: chuẩn bị theo split

Code: `run()` trong [pipeline.py](../src/datamate/agent/pipeline.py), [rules.py](../src/datamate/rules.py),
[sql/agent.py](../src/datamate/sql/agent.py).

1. **Đọc `rulebook.md` của split** đang trả lời (rulebook public và private khác train).
2. **`rule_params`**: đọc các con số macro cần từ chữ của rulebook, ví dụ giờ ca sáng/chiều. Nếu không đọc được thì tắt
   macro tương ứng (`params_off`).
3. **`rule_gate`**: so từng điều khoản mà macro viết tay dựa vào (`DEPENDS`) với rulebook mới. Câu chữ giống train thì giữ
   macro. Câu chữ khác thì hỏi LLM một lần xem nghĩa có đổi không; chỉ khi LLM trả lời "không" rõ ràng mới giữ. Thiếu điều
   khoản thì tắt. Kết quả là tập `off`, được cache ở `cache/ingest/<split>/rules.json`.
4. **`rule_delta`**: tìm các dòng luật rulebook này thêm hoặc đổi so với train (LLM xác nhận, cache ở `rule_delta.json`). Các
   dòng này dùng cho router (lý do "changed rule"). Với cấu hình nộp, chúng không được biên dịch thành macro (P4).
5. **System prompt** (`build_sql_system`) gồm:
   - cách truy vấn chạy: các view `sales_lines` và `orders` đã cắt tại `as_of()`; bảng `raw_*` phải tự lọc `<= as_of()`;
   - định dạng đầu ra: một khối ```sql, dòng đầu là `-- type: money|count|number|percent|boolean|string|date|month|list|table`,
     hoặc một dòng `UNANSWERABLE: lý do`;
   - với `--context rich`: 3 dòng kế hoạch `-- terms:`, `-- rule:`, `-- needs:` trước truy vấn;
   - quy tắc chung (rulebook thắng macro, xếp hạng giảm dần rồi theo mã tăng dần, tên chép đúng dữ liệu...);
   - các truy vấn mẫu, bỏ những mẫu gọi macro đã bị tắt;
   - **toàn bộ rulebook** của split;
   - `rule_note(off)`: danh sách định nghĩa đã tắt, yêu cầu mô hình theo rulebook.
6. **ExampleBank** ([retrieve.py](../src/datamate/agent/retrieve.py)): câu hỏi train cùng đáp án gold, lập chỉ mục
   TF-IDF n-gram ký tự trên văn bản bỏ dấu. Không có tham số học. Code đọc thêm SQL đúng từ `assets/examples_sql.jsonl`
   nếu file tồn tại, nhưng file này không có trong repo, nên ví dụ chỉ gồm câu hỏi và đáp án.
7. **Calendar** của split (âm lịch, ngày lễ).

---

## 3. Giai đoạn B: chuẩn bị theo shop

Chạy một lần cho mỗi shop, theo thứ tự tên shop. Lượt prepare-only của predict.sh làm sẵn và cache phần này.

### B1. Ingest: file thô thành bảng sạch

Code: [ingest/business.py](../src/datamate/ingest/business.py), [tables.py](../src/datamate/ingest/tables.py),
[parsers.py](../src/datamate/ingest/parsers.py), [roles.py](../src/datamate/ingest/roles.py),
[readme.py](../src/datamate/ingest/readme.py). Kết quả được cache theo `INGEST_VERSION = 23`.

1. Đọc mọi sheet (`python-calamine` cho xlsx, csv, sqlite).
2. Tìm dòng tiêu đề (bỏ dòng tựa, dòng lọc, dòng trống; ghép tiêu đề 2 dòng), bỏ dòng tổng phụ.
3. Gộp các mảnh cùng lược đồ (sheet theo tháng, file `_1/_2`, file theo năm) thành một bảng.
4. Gán **vai trò cột** (`order_id`, `order_date`, `line_amount`, `qty`, `product_name`, `status`, `order_goods`...): mô tả cột
   trong README được xét trước, tên cột xét sau.
5. `_clean`:
   - **điền ô trống theo rulebook §8**: dòng sau của một đơn lấy giá trị dòng đầu; cột chỉ ghi ở dòng đầu đơn
     (`_group_fill`); khối theo ngày không có mã đơn (`_block_fill`, P1b);
   - bỏ dòng trùng y hệt trong cùng một đơn (lỗi export).
6. Ép kiểu: ngày lẫn định dạng, số tiền (`55k`, `1,6tr`), số lượng kèm đơn vị, chuẩn hóa trạng thái.

### B2. Gọi LLM một lần mỗi shop, chỉ khi cần

- **`resolve_columns`** ([columns.py](../src/datamate/agent/columns.py)): chỉ chạy khi shop thiếu vai trò mà sales cần,
  hoặc không dựng được fact nào. Hỏi LLM về các cột chưa có vai trò. Đề xuất chỉ được nhận nếu giá trị cột qua kiểm tra
  ([ingest/trust.py](../src/datamate/ingest/trust.py)). Kết quả lưu cạnh cache ingest.
- **`resolve_statuses`** ([statuses.py](../src/datamate/agent/statuses.py)): giá trị trạng thái mà luật không nhận ra được LLM
  xếp vào một trong 4 nhãn: hoàn thành, huỷ, trả hàng, đang giao. Kết quả cũng được lưu cache.

Cả hai lời gọi dùng thinking off, temperature 0, ngân sách 400 token.

### B3. Bảng chuẩn và kiểm tra

- **`build_sales`** ([canon.py](../src/datamate/canon.py)): `sales_lines` có một dòng cho mỗi dòng hàng, nối với trường cấp
  đơn (ngày, trạng thái, khách) và thuộc tính sản phẩm. Cột `counted` tính sẵn luật doanh thu mặc định: đơn không huỷ, không
  trả và không có dòng trả. Shop chỉ có đơn (không có dòng hàng) lấy tiền hàng từ cột có vai trò `order_goods`.
- **`assess`** (trust): mỗi khả năng (revenue, orders, customers, products) có một trạng thái trusted, suspect hoặc absent.
  Revenue suspect thì tắt các kiểm tra chéo bằng macro (`shop_off`).
- **`build_facts`** ([facts.py](../src/datamate/facts.py)): các bảng fact ở đúng độ hạt của rulebook (nợ, thanh toán, chuyến
  giao, nhập hàng, chi phí, tồn kho, lô). Cột được tìm theo mô tả README trước, theo tên cột sau.
- **`profile`** ([capabilities.py](../src/datamate/capabilities.py)): trạng thái của từng khả năng (debt, deliveries,
  imports, stock...). Kết quả ghi vào `capabilities.json`.
- **`audit`** ([audit.py](../src/datamate/audit.py), P2): kiểm tra chéo không cần đáp án, không gọi LLM:
  - `role_gap`: README mô tả một cột mà ingest không gán vai trò;
  - `status_in_text`: trạng thái huỷ/trả nằm trong cột ghi chú;
  - `discount_unapplied`: thành tiền bằng SL × giá dù dòng có giảm giá;
  - `fill_gap`: ô cấp đơn vẫn trống ở dòng sau.

  Có cờ nào thì shop là `suspect`. Không shop train nào bị gắn cờ. Kết quả ghi vào `audit.json` và được dùng ở cổng C10.

### B4. DuckDB store và schema card

- **`ensure_store`** ([sql/store.py](../src/datamate/sql/store.py), `STORE_VERSION = 14`): mỗi shop có một file DuckDB chỉ
  đọc, gồm `sales_all`, `orders_all`, `dim_product`, `dim_customer`, `calendar`, các bảng `raw_<tên>`, các view fact và các
  **macro** (lớp ngữ nghĩa viết tay, kiểm thử trên train: `revenue`, `cancel_rate`, `debt_by_customer`, `price_effect`,
  `slow_products`, `lots_left`...). Macro chỉ tồn tại khi shop có dữ liệu cho nó. Các view `sales_lines` và `orders` cắt tại
  biến phiên `as_of`.
- **`SqlPool`**: 4 cursor, mỗi cursor giữ `as_of` riêng. Pool cũng có `ValueIndex` (giá trị của các cột phân loại: kênh,
  hình thức trả, nhóm hàng), danh sách đơn vị cơ sở, khoảng ngày của dữ liệu và các cột tiền cấp đơn.
- Macro bị tắt (`off`) được chặn (`pool.block`): truy vấn gọi tới sẽ nhận lỗi "does not exist".
- **Schema card** (`sql_card`): view `sales_lines` và `orders` với cột và giá trị mẫu, bảng raw (với `rich`: chỉ phần
  `sales_lines` chưa có), khối **Keys** (cách nối bảng raw với view), ghi chú ingest, danh sách macro, và capabilities
  (`rich`).
- `biz_part` = README của shop + schema card. Phần này giống nhau cho mọi câu của shop, nên prefix caching của vLLM dùng lại
  được.
- **`EntityIndex`** ([preprocess/entities.py](../src/datamate/preprocess/entities.py)): mọi tên sản phẩm, khách, nhân viên,
  nhà cung cấp, nhóm của shop, dùng để khớp tên trong câu hỏi.

---

## 4. Giai đoạn C: mỗi câu hỏi (`solve_question`)

### C1. Hạn giờ

- Đã qua `deadline`: trả `abstain` ngay, log có `"skipped": "deadline"`.
- Đã qua `deadline − deep_margin` (giờ cuối): bỏ tầng deep, nên không có router deep, leo thang hay judge.

### C2. Tiền xử lý câu hỏi

Code: [preprocess/__init__.py](../src/datamate/preprocess/__init__.py) (`rules_normalize`),
[rewrite.py](../src/datamate/preprocess/rewrite.py). Nguyên tắc: chỉ đổi cách viết, không đổi nội dung. Việc hiểu ý định là
bước khác.

**Bằng luật (luôn chạy, không gọi mô hình):**

| bước | ví dụ | file |
|---|---|---|
| clean: NFC, bỏ emoji và ký tự lạ, gộp khoảng trắng | | `preprocess/__init__.py` |
| telex | `soos` → `số`, `dduwowcj` → `được` | `telex.py` |
| tên trong dữ liệu shop: khớp, sửa dấu, bảo vệ khỏi các bước sau | `khach doan thao son` → tên đúng chính tả | `entities.py` |
| từ: viết tắt, teen-code, phương ngữ, tiếng Anh | `dthu` → `doanh thu`, `mô` → `nào` | `lexicon.py` (chỉ từ có trong câu hỏi train hoặc nguồn ngoài) |
| số tiền, ngày | `3tr7` → `3.700.000 đồng`, `th7/26` → `tháng 7/2026` | `amounts.py` |

Kết quả là **văn bản luật (rules text)**, đầu vào của mọi bước code sau.

**LLM rewrite (có điều kiện, `--rewrite residual`):** dựng thử frame (C3) trên văn bản luật. Chỉ khi còn từ chưa giải thích
được (residual) mới gọi một lần LLM, với thinking off, temperature 0, tối đa 400 token. LLM viết lại thành tiếng Việt chuẩn
và trả JSON gồm `normalized`, kiểu đáp án, top-k, `sub_questions`. Bản viết lại chỉ được dùng nếu giữ đủ mọi con số và mọi
tên khớp chính xác; nếu không thì bị loại, chỉ giữ dự đoán kiểu. Lời gọi lỗi chỉ làm mất bản viết lại.

### C3. Code đọc câu hỏi

| thành phần | code | dùng để |
|---|---|---|
| `expect_answer` | [postprocess.py](../src/datamate/postprocess.py) | kiểu đáp án mong đợi; mẫu chính xác ≥ 99% trên train là `hard`, dự đoán của rewrite là `soft` |
| `values.hints` | [sql/values.py](../src/datamate/sql/values.py) | cụm từ trong câu khớp giá trị cột (`tiền mặt` → `"Trả bằng" = 'cash'`) |
| `subjective_note` | sql/values.py | ghi chú khi câu hỏi mang tính chủ quan |
| `pick_rules` | [agent/evidence.py](../src/datamate/agent/evidence.py) | 5 dòng rulebook trùng từ nhiều nhất với câu hỏi (`rich`) |
| `bank.query` | retrieve.py | 4 ví dụ train gần nhất **của shop khác**, bỏ ví dụ cần khả năng mà shop này `absent` |
| `bank.nearest` | retrieve.py | độ giống với câu train gần nhất, dùng cho router |
| `time_hints` | [agent/hints.py](../src/datamate/agent/hints.py) | giải kỳ thời gian theo rulebook §2 (tháng này, tuần trước, N ngày gần nhất, Tết...) ra ngày cụ thể |
| `build_frame` | [agent/frame.py](../src/datamate/agent/frame.py) | khung câu hỏi: đại lượng và macro, kỳ, tên, giá trị, dạng đáp án, khả năng cần, từ dư (residual) |
| `date_window` | postprocess.py | cửa sổ ngày để kiểm tra ngày trong đáp án |
| `shadow` | `sql_shadow` | (câu hỏi, từ, đến) khi câu chỉ có một kỳ: macro dùng để kiểm tra chéo đáp án |

### C4. Dựng prompt

Mỗi lời gọi solver gồm một system message và một user message (2 phần nối nhau):

```
system:  SYSTEM_TEMPLATE SQL (+ dòng kế hoạch) + RULEBOOK của split + rule_note(off)
user:    [biz_part]       # Business <id>: README  +  Schema card (views, raw, Keys, macro, capabilities)
         [question_part]  # Solved examples from OTHER shops (Q, ```sql```, A) × 4
                          # Question: as_of_date (hôm nay) + thứ, question gốc
                          # normalized / asks, in this order / expected answer (nếu hard)
                          # names in this shop's data / amounts
                          # Values in this shop's data ...      (values.hints)
                          # Note: ...                           (subjective)
                          # RULEBOOK lines whose words ...      (pick_rules)
                          # Frame of the question ...           (--frame)
                          # Time expressions, already resolved  (time_hints, ra ngày)
```

Prompt khoảng 13–16k token, phần lớn là rulebook cùng README và card. System message và `biz_part` giống nhau cho mọi câu của
một shop.

### C5. Router thinking (`route_reasons`)

Câu hỏi vào **tầng deep** (thinking on, 8192 token) nếu có một trong các lý do:

- "changed rule": câu chạm một định nghĩa bị tắt, hoặc dòng rulebook liên quan nằm trong `rule_delta`;
- "suspect sales": revenue của shop là suspect (trust);
- "unfamiliar": câu train gần nhất của shop khác có độ giống < 0.75;
- "suspect <khả năng>": khả năng mà câu cần đang ở trạng thái suspect;
- "residual N": frame còn ≥ 3 từ chưa giải thích.

Không có lý do nào thì câu ở **tầng normal**: thinking off, 2048 token.

### C6. LLM viết SQL, chạy, kiểm tra, sửa (`_samples`)

Một mẫu greedy (temperature 0, seed 0), tối đa 2 lượt (1 lượt đầu + 1 lượt sửa):

1. **Gọi LLM** (`LocalLLM.generate`): API OpenAI-compatible của vLLM, `chat_template_kwargs.enable_thinking` theo tầng.
   Lỗi kết nối hoặc 5xx được thử lại với backoff 5–120 s, tối đa 6 lần; sau đó cả lượt chạy dừng (`LLMUnavailable`) mà không
   ghi các câu còn lại, để lượt khởi động lại trả lời chúng. Lỗi 4xx chỉ làm hỏng câu đó.
2. **`extract_sql`**: lấy khối ```sql (kể cả khối mở thiếu dấu đóng), hoặc dòng `UNANSWERABLE:`.
3. **`term_advice`** (chỉ ở lượt đầu): câu hỏi là một thuật ngữ đã có macro kiểm thử (nợ, lợi nhuận, chuyến giao...) mà truy
   vấn tự dựng lại từ bảng raw thì yêu cầu viết lại một lần. Lượt thứ hai chạy đúng như mô hình viết.
4. **`SqlPool.run` → `execute`** ([sql/agent.py](../src/datamate/sql/agent.py)):
   - dòng `UNANSWERABLE` → kết quả `unanswerable`;
   - thiếu dòng `-- type:` → lỗi;
   - `LIMIT` không có `ORDER BY` → lỗi;
   - gọi macro bị chặn → lỗi;
   - **viết lại tự động**: sửa cách gọi macro (`rewrite_macros`: macro TABLE dùng như scalar, `100 * cancel_rate(...)`...),
     thêm điều kiện bỏ khách lẻ khi xếp hạng khách (`add_not_walkin`), thêm điều kiện bỏ dòng hàng không tên
     (`add_named_products`);
   - **lint**: sai độ hạt (đếm đơn trên `sales_lines`), tiền cấp đơn cộng trên dòng, lọc `=` trên cột ghi chú tự do...;
   - đặt `as_of`, chạy SELECT với timeout 30 s; lỗi "column not found" thì thử nối lại khóa (`rebind_key`, tối đa 3 lần);
   - `to_answer`: ép kết quả theo `-- type:` thành đáp án có kiểu;
   - `check_result`: tên có trong dữ liệu không, kết quả 0 hay rỗng, kỳ nằm ngoài dữ liệu;
   - **kiểm tra chéo bằng macro (shadow)**: nếu câu có đúng một kỳ và macro tương ứng còn bật thì so đáp án với macro. Lệch
     thì thành cảnh báo; macro cho ra `contract`. Có thêm kiểm tra chéo theo đơn vị ("bao nhiêu ly").
5. **Hậu kiểm**: `fit_month` ('YYYY-MM' khi hỏi tháng), `fit_money` (làm tròn tiền), `check_answer` (đối chiếu kiểu mong
   đợi), `invariants` (luôn đúng trên gold train, nên vi phạm là lỗi cứng).
6. **Sửa**: có lỗi thì gửi `sql_repair_message`; có cảnh báo thì gửi `sql_check_message` (mô hình trả lại truy vấn, đã sửa
   hoặc giữ nguyên nếu đúng). Câu trả lời trước và tin nhắn sửa được thêm vào history.
   - Trường hợp đặc biệt: thinking dùng hết ngân sách nên câu trả lời rỗng (`finish_reason = length`). Lượt sau gọi lại với
     thinking off.
7. **Kết thúc mẫu**: cảnh báo cứng vẫn còn sau lượt sửa thì kết quả bị bỏ (lỗi `unconfirmed`). Nếu mẫu không có kết quả mà
   macro đã cho `contract`, dùng `contract`.

### C7. Vote (`decide`)

Với 1 mẫu, vote chỉ là: có kết quả thì giữ, không có thì `abstain`. Khi chạy nhiều mẫu, UNANSWERABLE cần ≥ 60% số phiếu,
câu trả lời cần ≥ 50%. Phát hiện **rào đón** (`doubt`): mô hình trả lời nhưng cũng viết UNANSWERABLE trong câu trả lời.

### C8. Leo thang (`escalate_reasons`)

Câu ở tầng normal được làm lại toàn bộ C6–C7 ở tầng deep nếu kết quả đầu có dấu hiệu sai: `abstain`, `unanswerable`, rào đón,
cảnh báo kiểm tra, hoặc bị cắt vì `length`. Kết quả deep thay kết quả đầu. Log giữ cả hai trong `route.first`, cùng cờ
`agree` cho biết hai kết quả có trùng nhau không.

### C9. Judge (`_judge`, [agent/judge.py](../src/datamate/agent/judge.py))

Chỉ chạy ở tầng deep hoặc escalated, khi kết quả là UNANSWERABLE hoặc có rào đón. Judge đọc rulebook, README, card, câu hỏi
cùng phán quyết và truy vấn của solver, rồi trả JSON `{answerable, needs, where}`:

| solver | judge | kết quả |
|---|---|---|
| trùng ý | | giữ |
| UNANSWERABLE | dữ liệu có | solver thử thêm 1 lần với gợi ý của judge; ra đáp án hợp lệ thì lấy, không thì `abstain` |
| trả lời, có rào đón | dữ liệu thiếu | `unanswerable` |
| trả lời, không rào đón | dữ liệu thiếu | `abstain` |

### C10. Cổng quyết định (`gates.apply`, [agent/gates.py](../src/datamate/agent/gates.py))

Các luật xác định, chạy trên log đã xong. Mỗi cổng đã được đo trên lượt chạy train trước khi bật. Cổng đầu tiên khớp sẽ quyết
định:

| cổng | điều kiện | kết quả |
|---|---|---|
| `shop_suspect` | shop bị audit gắn `suspect` và kiểu đáp án là number, percent, string, list hoặc table | abstain (date, boolean, unanswerable giữ nguyên) |
| `unans_in_sql` | đang abstain, lượt cuối có dòng UNANSWERABLE nằm trong khối SQL, và judge không chọn abstain | unanswerable |
| `count_money` | hỏi "bao nhiêu đơn/khách/ly..." (không có từ chỉ tiền) mà đáp án ≥ 1.000.000 | abstain |
| `items_len` | hỏi "bao nhiêu món/mặt hàng" (không hỏi tên) mà đáp án là list | number = số phần tử |
| `table_words` | đáp án dạng table mà câu hỏi không có các từ kèm, bảng, từng, mỗi người, top | abstain |
| `expect_type` | kiểu `hard` mong đợi khác kiểu đáp án | abstain |

Log ghi `gated = {gate, from}` để biết đáp án gốc. Calibrator (L8) có trong code nhưng không chạy ở cấu hình nộp (xem mục 8).

### C11. Finalize và ghi

`finalize` ([postprocess.py](../src/datamate/postprocess.py)) đưa đáp án về đúng định dạng chính thức:

- number là số JSON;
- percent làm tròn 2 chữ số;
- date phải là `YYYY-MM-DD` hoặc `YYYY-MM`;
- list không được rỗng;
- table phải có `columns` và `rows`; bảng theo cột được xoay thành theo dòng.

Sai định dạng thì thành `abstain`.

---

## 5. Output

Mỗi câu được ghi ngay khi xong, có khóa, `flush` sau từng dòng, nên nếu bị dừng ở giới hạn giờ thì các câu đã xong vẫn còn.

| file | nội dung |
|---|---|
| `submission.jsonl` | mỗi dòng `{"id", "type", "answer"}` hoặc `{"id", "type": "unanswerable", "reason"}` hoặc `{"id", "type": "abstain"}` |
| `logs/<id>.json` | đầu file có các trường guideline yêu cầu: `id`, `answer`, `elapsed_ms`, `thinking` (danh sách hoặc null), `code` (các truy vấn đã chạy, gồm lượt thử lại của judge), `output` (kết quả hoặc `{"error"}`); sau đó là log đầy đủ: `pre` (rules, changes, mentions, rewrite, norm), `expect`, `examples`, `near`, `hints`, `samples[].attempts[]` (reply, code, ran, result, error, warning, thinking, finish_reason), `vote`, `route`, `judge`, `frame`, `consistency`, `magnitude`, `audit`, `gated`, `elapsed` |
| `config.json` | toàn bộ RunConfig cùng semantic, ingest và store version |
| `capabilities.json` | trạng thái khả năng của từng shop |
| `audit.json` | cờ audit và các tỉ lệ đo được của từng shop |
| `summary.json`, `report.json` | chỉ khi split có `answers.jsonl` (train): PA và kết quả từng câu |
| `predict.log`, `vllm.log`, `part<i>.log` | log của predict.sh |

Đo lại cổng trên một lượt đã chạy: `uv run datamate gate <run_dir> --gold <answers.jsonl>`. Lệnh phát lại từ trường `from`, nên
so được trước và sau cổng.

---

## 6. Số lời gọi LLM

| thời điểm | lời gọi | thinking | token tối đa |
|---|---|---|---|
| mỗi split | `rule_gate`: 1 cho mỗi điều khoản có câu chữ khác train; `rule_delta`: theo dòng đổi (cache) | off | 400 |
| mỗi shop | `resolve_columns` (nếu thiếu vai trò), `resolve_statuses` (nếu có trạng thái lạ) (cache) | off | 400 |
| mỗi câu, tiền xử lý | rewrite: 0 hoặc 1 (chỉ khi còn residual) | off | 400 |
| mỗi câu, normal | solver: 1–2 (lượt đầu + sửa) | off | 2048 |
| mỗi câu, deep / leo thang | solver: thêm 1–2 | on | 8192 |
| mỗi câu, judge | 1, và thêm 1 lượt solver nếu judge thấy dữ liệu có | on | 8192 |

Câu dễ: 1 lời gọi. Câu xấu nhất: khoảng 7 lời gọi (rewrite + 2 normal + 2 deep + judge + thử lại).

---

## 7. Bảng tra: bước → file

| bước | file chính |
|---|---|
| điểm vào Docker | [docker/predict.sh](../docker/predict.sh) |
| CLI | [src/datamate/cli.py](../src/datamate/cli.py) (`run`, `gate`, `audit`, `score`, `sql-check`...) |
| điều phối | [src/datamate/agent/pipeline.py](../src/datamate/agent/pipeline.py) |
| rulebook → cổng định nghĩa | [src/datamate/rules.py](../src/datamate/rules.py) |
| ingest | [src/datamate/ingest/](../src/datamate/ingest/) |
| vai trò cột / trạng thái bằng LLM | [agent/columns.py](../src/datamate/agent/columns.py), [agent/statuses.py](../src/datamate/agent/statuses.py) |
| sales_lines, facts, trust, capabilities, audit | [canon.py](../src/datamate/canon.py), [facts.py](../src/datamate/facts.py), [ingest/trust.py](../src/datamate/ingest/trust.py), [capabilities.py](../src/datamate/capabilities.py), [audit.py](../src/datamate/audit.py) |
| DuckDB store, macro | [sql/store.py](../src/datamate/sql/store.py), [sql/dims.py](../src/datamate/sql/dims.py) |
| tiền xử lý | [preprocess/](../src/datamate/preprocess/) |
| kỳ thời gian, frame, ví dụ, dòng rulebook | [agent/hints.py](../src/datamate/agent/hints.py), [agent/frame.py](../src/datamate/agent/frame.py), [agent/retrieve.py](../src/datamate/agent/retrieve.py), [agent/evidence.py](../src/datamate/agent/evidence.py) |
| prompt SQL, card, chạy truy vấn, lint, viết lại | [sql/agent.py](../src/datamate/sql/agent.py), [agent/prompt.py](../src/datamate/agent/prompt.py) |
| client LLM | [agent/llm_local.py](../src/datamate/agent/llm_local.py) |
| kiểm tra đáp án, finalize | [postprocess.py](../src/datamate/postprocess.py) |
| vote, judge, gates | [agent/vote.py](../src/datamate/agent/vote.py), [agent/judge.py](../src/datamate/agent/judge.py), [agent/gates.py](../src/datamate/agent/gates.py) |

---

## 8. Có trong code nhưng không chạy ở cấu hình nộp

| tính năng | cờ | lý do tắt |
|---|---|---|
| biên dịch macro cho luật mới/đổi | `--compile-delta` | public: 4/176 câu gọi tới, không câu nào đúng, tốn 21–28 phút prepare (P4) |
| calibrator logistic (L8) | `--calibrator` | fit trên lượt chạy train (leave-business-out) thì ngưỡng 0.5 không loại câu nào |
| fast branch (trả lời không cần mô hình) | `--fast` | không dùng |
| shop views do mô hình viết | `--shop-views` | không dùng |
| nhiều mẫu + vote | `--samples N` | tốn thời gian; 1 mẫu greedy giúp tái lập kết quả |
| catalog `primitives` / `compiled` | `--catalog` | thử nghiệm: mô hình tự tính mọi thuật ngữ |
| engine pandas | `--engine pandas` | đã dừng từ 2026-11-08 |
| backend Claude / Gemini | `--backend` | chỉ dùng để phát triển trên train |
| shop gate cũ dựa trên tỉ lệ | `shop-gate-fit`, `shop-gate` | thay bằng audit + cổng `shop_suspect` |
