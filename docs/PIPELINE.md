# Pipeline DataMate: bản nộp cuối (2026-11-15)

Tài liệu này mô tả pipeline đúng như code của bản nộp cuối trên nhánh `text2sql`. Cấu hình nộp nằm trong
[docker/predict.sh](../docker/predict.sh). Sau bản này, code không sửa thêm.

- Mục 0 là kết quả.
- Mục 1–7 đi theo thứ tự chạy, từ một câu hỏi đến submission.
- Mục 8 ghi phần kiểm chứng và cơ sở tuân thủ của từng thay đổi.
- Mục 9 là các lỗi còn lại và lý do chưa sửa.
- Mục 10–11 là bảng tra file và các tính năng có trong code nhưng tắt.

---

## 0. Kết quả

PA chấm bằng `ground_truth.jsonl` ở máy. File này không có `alias`, nên điểm chính thức thường cao hơn một chút: public_e
được 0.716 ở máy và 0.724 trên leaderboard. Các lượt chạy dùng Qwen3.5-9B trên 1× RTX 5090 thuê, với các cờ nộp ở mục 2.

| lượt | thay đổi so với lượt trước | đúng / bỏ qua / sai | PA |
|---|---|---|---|
| public_d | trước slots | 649 / 297 / 54 | 0.595 |
| public_e | slots `order_key`, `status_text`, `name_map`, `line_amount` | 792 / 132 / 76 | 0.716 (LB 0.724) |
| public_g | `shop_suspect` thu hẹp theo phạm vi, thêm lượt thử sau khi thinking bị cắt, gợi ý schema khi lỗi Binder/Catalog | 869 / 37 / 94 | 0.775 |
| public_h | 131 câu chưa đúng của g cộng 60 câu đối chứng; thinking 16384, `customer_group`, phạm vi `Trả góp`; ghép vào g | 882 / 40 / 78 | 0.804 |
| public_i | toàn bộ public: mọi thay đổi trên, slot `unit_factor`, thinking 16384 | 857 / 75 / 68 | 0.789 |
| **bản cuối** | public_i, cờ `unit_gap` không còn làm shop `suspect` (phát lại cổng trên log của public_i, đổi 24 câu) | **874 / 51 / 75** | **0.799** |

Trên train, các thay đổi của bản cuối không làm gì cả:
- `sql-check`: 826/859, bằng baseline;
- `facts-check`: không có câu trượt;
- không slot nào kích hoạt trên 30 shop train;
- không shop train nào bị audit gắn cờ;
- 411 unit test đều qua.

---

## 1. Sơ đồ tổng

```
predict.sh
 ├─ khởi động vLLM (Qwen3.5-9B, TP=2, bf16)
 ├─ lượt prepare-only: chuẩn bị mọi shop một lần (cache)
 └─ 12 tiến trình `datamate run`, mỗi tiến trình một nhóm shop, cùng ghi vào /result

datamate run  (src/datamate/agent/pipeline.py: run)
 A. Theo split (một lần)
    rulebook ─► rule_params, rule_gate, rule_delta ─► system prompt SQL
    câu hỏi train + đáp án gold ─► ExampleBank
 B. Theo shop (một lần mỗi shop)
    file thô ─► ingest ─► (LLM: vai trò cột, trạng thái đơn)
             ─► slots (LLM điền JSON hẹp, code áp dụng): order_key, status_text, name_map, line_amount, unit_factor
             ─► sales_lines ─► trust, facts, capabilities, audit (cờ + phạm vi)
             ─► DuckDB store ─► SqlPool + schema card + EntityIndex
 C. Mỗi câu hỏi  (solve_question, 10 câu song song mỗi tiến trình)
    C1 hạn giờ
    C2 tiền xử lý: luật ─► (LLM rewrite nếu còn từ chưa giải thích)
    C3 code đọc câu hỏi: kiểu đáp án, giá trị, dòng rulebook, kỳ thời gian, ví dụ, frame
    C4 dựng prompt
    C5 router: tầng normal (thinking off) hay deep (thinking on)
    C6 LLM viết SQL ─► chạy trên DuckDB (ghi lại bảng/cột đã đọc) ─► kiểm tra ─► sửa
    C7 vote
    C8 leo thang lên deep nếu câu trả lời đầu có dấu hiệu sai
    C9 judge (agent thứ hai) cho UNANSWERABLE / câu trả lời có rào đón
    C10 cổng quyết định (gates) ─► abstain hoặc sửa dạng
    C11 finalize ─► submission.jsonl + logs/<id>.json
```

---

## 2. Điểm vào và cấu hình nộp

[docker/predict.sh](../docker/predict.sh) không nhận tham số. Các bước:

1. Ghi `T0`, đặt `DEADLINE = T0 + 6h − 25 phút` và `DEEP_MARGIN = 3600 s`.
2. Tạo `/result/submission.jsonl` rỗng ngay từ đầu: thiếu dòng chỉ là bỏ qua, còn thiếu file thì cả lượt không hợp lệ.
3. Liên kết `/data` thành `<WORK>/root/test`, vì `/data` chỉ đọc được. Cache và DuckDB ghi vào `/tmp/datamate`.
4. Khởi động vLLM: `--tensor-parallel-size 2 --dtype bfloat16 --max-model-len 49152 --gpu-memory-utilization 0.90
   --max-num-seqs 128 --max-num-batched-tokens 16384 --enable-prefix-caching --reasoning-parser qwen3
   --language-model-only --seed 0`.
5. Chạy `datamate run --prepare-only` một lần để làm toàn bộ việc theo shop và cache lại: ingest, các lời gọi LLM cho vai trò
   cột, trạng thái đơn và slots, dựng store.
6. Chia shop thành `NGROUPS = 12` nhóm, cân theo số câu hỏi. Biến này không đặt tên là `GROUPS`, vì đó là mảng sẵn có của
   bash và bỏ qua phép gán: image #1 hỏng vì lỗi này. Log in ra danh sách nhóm. Mỗi nhóm chạy `datamate run --biz <shop>
   --concurrency 10`, tất cả cùng ghi vào `/result`.
7. Nếu vLLM chết thì khởi động lại, tối đa 4 lần. Lượt sau bỏ qua các câu đã có trong submission (`resume`).

Image Docker: base `vllm/vllm-openai:v0.32.0-cu129`, có thêm `pip uninstall -y torchcodec`. Base kèm torchcodec bản cu130;
khi import, nó ném `OSError` mà vLLM 0.32 không bắt, nên `vllm serve` chết. Đây là lỗi thứ hai của image #1.

Cờ của `datamate run` trong lượt nộp:

| cờ | ý nghĩa |
|---|---|
| `--engine sql` | mô hình viết một câu SELECT DuckDB |
| `--thinking route` | mặc định tắt thinking; câu nào được router chọn thì bật |
| `--max-tokens 2048` | ngân sách token của tầng normal |
| `--think-max-tokens 16384` | ngân sách của tầng deep, judge và slots |
| `--catalog delta` | dùng macro viết tay; macro của luật bị rulebook đổi thì tắt; không biên dịch macro mới |
| `--judge unans` | agent thứ hai kiểm tra phán quyết UNANSWERABLE và câu trả lời có rào đón |
| `--context rich` | card có khóa nối bảng, fact còn thiếu, capabilities; prompt có các dòng rulebook liên quan; truy vấn mở đầu bằng 3 dòng kế hoạch |
| `--frame` | in khung câu hỏi vào prompt |
| `--rewrite residual` | chỉ gọi LLM rewrite khi bản đã làm sạch bằng luật còn từ chưa giải thích được |
| `--exec-workers 4` | 4 cursor DuckDB mỗi shop |
| `--deadline`, `--deep-margin` | hạn giờ tuyệt đối (epoch) và lề để tắt tầng deep |
| (mặc định) `--samples 1 --repair 1 --slots all` | 1 mẫu greedy (temperature 0), 1 lượt sửa, mọi slot bật |
| (mặc định) gates bật | cổng quyết định ở bước C10 |

---

## 3. Giai đoạn A: chuẩn bị theo split

Code: `run()` trong [pipeline.py](../src/datamate/agent/pipeline.py), [rules.py](../src/datamate/rules.py),
[sql/agent.py](../src/datamate/sql/agent.py).

1. **Đọc `rulebook.md` của split** đang trả lời. Rulebook public và private khác rulebook train.
2. **`rule_params`**: đọc từ chữ của rulebook các con số mà macro cần, ví dụ giờ ca sáng/chiều. Con số nào không đọc được thì
   tắt macro tương ứng.
3. **`rule_gate`**: so từng điều khoản mà macro viết tay dựa vào với rulebook mới.
   - Câu chữ giống train: giữ macro.
   - Câu chữ khác: hỏi LLM một lần xem nghĩa có đổi không, và chỉ giữ macro khi LLM trả lời "không" rõ ràng.
   - Thiếu điều khoản: tắt macro.

   Kết quả là tập `off`, cache ở `cache/ingest/<split>/rules.json`.
4. **`rule_delta`**: tìm các dòng luật mà rulebook này thêm hoặc đổi so với train (LLM xác nhận, có cache). Router dùng các dòng
   này cho lý do "changed rule".
5. **System prompt** (`build_sql_system`):
   - các view `sales_lines` và `orders` đã cắt tại `as_of()`;
   - định dạng đầu ra: dòng `-- type:` hoặc dòng `UNANSWERABLE:`;
   - 3 dòng kế hoạch `-- terms / -- rule / -- needs`;
   - các quy tắc chung;
   - truy vấn mẫu;
   - **toàn bộ rulebook** của split;
   - danh sách định nghĩa đã tắt.
6. **ExampleBank** ([retrieve.py](../src/datamate/agent/retrieve.py)): câu hỏi train cùng đáp án gold, chỉ mục TF-IDF n-gram
   ký tự trên văn bản bỏ dấu, không có tham số học.
7. **Calendar** của split (âm lịch, ngày lễ).

---

## 4. Giai đoạn B: chuẩn bị theo shop

Chạy một lần cho mỗi shop. Lượt prepare-only của predict.sh làm sẵn phần này.

### B1. Ingest: file thô thành bảng sạch

Code: [ingest/](../src/datamate/ingest/), cache theo `INGEST_VERSION = 23`.

1. Đọc mọi sheet: `python-calamine` cho xlsx, cùng csv và sqlite.
2. Tìm dòng tiêu đề (bỏ dòng tựa, dòng lọc, dòng trống; ghép tiêu đề 2 dòng) và bỏ dòng tổng phụ.
3. Gộp các mảnh cùng lược đồ (sheet theo tháng, file `_1/_2`, file theo năm).
4. Gán **vai trò cột** (`order_id`, `order_date`, `line_amount`, `qty`, `product_name`, `status`, ...): mô tả cột trong README
   xét trước, tên cột xét sau.
5. Điền ô trống theo rulebook §8: dòng sau của một đơn lấy giá trị dòng đầu; khối theo ngày không có mã đơn cũng được điền. Bỏ
   dòng trùng y hệt trong cùng một đơn.
6. Ép kiểu: ngày lẫn định dạng, số tiền (`55k`, `1,6tr`), số lượng kèm đơn vị, chuẩn hóa trạng thái.

### B2. LLM một lần mỗi shop, chỉ khi cần

- **`resolve_columns`** ([columns.py](../src/datamate/agent/columns.py)): hỏi LLM vai trò của các cột chưa có vai trò. Đề xuất chỉ
  được nhận khi giá trị cột qua kiểm tra ([ingest/trust.py](../src/datamate/ingest/trust.py)).
- **`resolve_statuses`** ([statuses.py](../src/datamate/agent/statuses.py)): giá trị trạng thái lạ được xếp vào một trong
  4 nhãn: hoàn thành, huỷ, trả hàng, đang giao.

### B3. Slots: quy ước riêng của shop, LLM đọc, code áp dụng

Code: [slots/base.py](../src/datamate/slots/base.py) (trigger, prompt, parse, check),
[slots/apply.py](../src/datamate/slots/apply.py) (áp dụng thuần, không gọi mô hình).

Nguyên tắc:
- Một slot chỉ chạy khi **dữ liệu có khoảng trống**, chứ không dựa vào chuyện một luật biết mình sai.
- LLM điền một JSON hẹp. Code chỉ giữ cột, giá trị và nhãn có thật, áp dụng, rồi **kiểm tra vật lý** kết quả. Không qua thì bỏ
  slot, store giữ nguyên như luật dựng, nên slot chỉ có thể thêm chứ không làm hỏng.
- Slot chạy theo thứ tự, slot sau thấy kết quả của slot trước.
- Kết quả cache ở `<biz>.slots.json`; prompt không đổi thì dùng lại câu trả lời cũ.
- Lời gọi dùng thinking on với ngân sách `--think-max-tokens`. Nếu thinking bị cắt mà câu trả lời rỗng, hỏi lại với thinking off.

| slot | kích hoạt khi | LLM điền | code áp dụng | kiểm tra |
|---|---|---|---|---|
| `order_key` | bảng dòng hàng không có cột mã đơn nhưng có cột có dạng mã đơn | `{order_column}` | cột đó thành vai trò `order_id` lúc ingest | store có mã đơn, không mất quá 5% dòng |
| `status_text` | mọi đơn đều "hoàn thành" trong khi có cột chữ chưa có vai trò | `{status_column, status_map}` | đơn nhận trạng thái xấu nhất trong các dòng của nó | 0 < tỉ lệ đơn huỷ/trả ≤ 30% |
| `name_map` | ≥ 30% dòng hàng có tên không thuộc danh mục nào, và có bảng nhỏ chứa các tên đó | `{table, short_col, full_col}` | tên viết tắt đổi thành tên đầy đủ (cột `(as written)` giữ bản gốc) | tỉ lệ tên lạ giảm ít nhất một nửa |
| `line_amount` | cột giảm giá > 0 mà thành tiền vẫn bằng SL × giá | `{discount_column, kind, level}` | trừ giảm giá dòng | thành tiền nằm trong [0, SL × giá]; doanh thu lệch < 20% |
| `unit_factor` | dòng bán bằng đơn vị khác đơn vị cơ bản mà `qty_base = qty` (`canon.unit_gap`) | `{base_qty_column}` hoặc `{factors: [{product, unit, factor}]}` | đặt `qty_base`; các hệ số này cũng dùng cho sổ nhập (`facts._units`) | số dòng thiếu hệ số giảm ít nhất một nửa; giá mỗi đơn vị cơ bản sau quy đổi nằm trong 0,5–1,5 lần giá bán lẻ |

`unit_gap` bỏ qua sản phẩm đã có dòng được quy đổi: khi đó các dòng còn lại của nó đã ở đơn vị cơ bản, chỉ có nhãn bị sai (train
biz_106). Đơn vị của dòng lấy từ vai trò `unit`, hoặc từ một cột chữ chưa có vai trò có giá trị là các từ đơn vị cơ bản của shop.

Trên public, slots kích hoạt ở:

| shop | slots được áp dụng |
|---|---|
| dev_003 | `order_key` (`Đơn số`), `status_text` (`Trạng thái giao`), `unit_factor` (cột `SL (ĐV cơ bản)`) |
| dev_004 | `unit_factor` (`xe 4m3` = 4, `thiên` = 1000…) |
| dev_011 | `name_map`, `status_text` |
| dev_013 | `status_text` (`Ghi chú`), `unit_factor` (thùng = 4/6 bịch…) |
| dev_014 | `name_map`, `order_key`, `status_text` |

### B4. Bảng chuẩn và kiểm tra

- **`build_sales`** ([canon.py](../src/datamate/canon.py)): `sales_lines` có một dòng cho mỗi dòng hàng, nối với trường cấp đơn
  và thuộc tính sản phẩm.
  - Cột `counted` tính sẵn luật doanh thu mặc định.
  - `attrs["sources"]` ghi các bảng nguồn, dùng cho phạm vi của audit.
  - Thuộc tính lấy từ danh mục sản phẩm (ví dụ cột `Nhóm` của nhóm hàng) không bao giờ được dùng làm `customer_group`.
  - Nếu vẫn còn dòng thiếu hệ số quy đổi, mô tả của card có dòng `WARNING` nói rằng `qty_base` của các dòng đó không theo
    đơn vị cơ bản.
- **`assess`** (trust): mỗi khả năng (revenue, orders, customers, products) có trạng thái trusted, suspect hoặc absent.
- **`build_facts`** ([facts.py](../src/datamate/facts.py)): các bảng fact ở đúng độ hạt của rulebook (nợ, thanh toán, chuyến
  giao, nhập hàng, chi phí, tồn kho, lô, đơn vị).
- **`profile`** ([capabilities.py](../src/datamate/capabilities.py)): trạng thái của từng khả năng.
- **`audit`** ([audit.py](../src/datamate/audit.py)): kiểm tra chéo không cần đáp án và không gọi LLM. Mỗi cờ ghi **phạm vi**
  của nó:

  | cờ | phát hiện | phạm vi |
  |---|---|---|
  | `role_gap` | README mô tả một cột mà ingest không gán vai trò | bảng đó |
  | `status_in_text` | trạng thái huỷ/trả nằm trong cột ghi chú | toàn bộ sales |
  | `discount_unapplied` | thành tiền bằng SL × giá dù dòng có giảm giá | toàn bộ sales |
  | `fill_gap` | ô cấp đơn vẫn trống ở dòng sau, hoặc bảng có bố cục điền từ dòng đầu (**mọi** bảng như vậy) | cột đó nếu cột chưa có vai trò, nếu không thì cả bảng |
  | `unit_gap` | vẫn còn ≥ 5 dòng thiếu hệ số quy đổi sau slots | chỉ ghi lại: không làm shop `suspect` |

  `gates.scope_of` đổi phạm vi thành tên trong store:
  - phạm vi bảng: `raw_<bảng>`, các bảng fact, và mọi bảng sales nếu sales đọc từ bảng đó;
  - phạm vi cột: tên cột.

  Kết quả ghi vào `audit.json`. Không shop train nào bị gắn cờ.

### B5. DuckDB store và schema card

- **`ensure_store`** ([sql/store.py](../src/datamate/sql/store.py), `STORE_VERSION = 17`): mỗi shop một file DuckDB chỉ đọc.
  - Gồm `sales_all`, `orders_all`, `dim_product`, `dim_customer`, `calendar`, các bảng `raw_<tên>`, các bảng fact, và các
    **macro** (lớp ngữ nghĩa viết tay, kiểm thử trên train).
  - Các view `sales_lines` và `orders` cắt tại biến phiên `as_of`.
  - Tên file store có hash của các nhãn LLM đã lưu (vai trò, trạng thái, slots), nên nhãn đổi thì store được dựng lại.
- **`SqlPool`**: 4 cursor, mỗi cursor giữ `as_of` riêng. Pool có thêm `ValueIndex`, danh sách đơn vị cơ bản, khoảng ngày của
  dữ liệu và các cột tiền cấp đơn. Macro bị tắt được chặn.
- **Schema card** (`sql_card`): view và cột kèm giá trị mẫu, bảng raw, khối Keys, ghi chú ingest và slots, macro, capabilities.
- `biz_part` = README của shop + schema card, giống nhau cho mọi câu của shop (prefix caching).
- **`EntityIndex`**: mọi tên sản phẩm, khách, nhân viên, nhà cung cấp, nhóm, dùng để khớp tên trong câu hỏi.

---

## 5. Giai đoạn C: mỗi câu hỏi (`solve_question`)

### C1. Hạn giờ

- Đã qua `deadline`: trả `abstain`, log có `"skipped": "deadline"`.
- Đã qua `deadline − deep_margin`: bỏ tầng deep (không router deep, không leo thang, không judge).

### C2. Tiền xử lý câu hỏi

Code: [preprocess/](../src/datamate/preprocess/). Nguyên tắc: chỉ đổi cách viết, không đổi nội dung.

**Bằng luật (luôn chạy):**
- clean: NFC, bỏ emoji, gộp khoảng trắng;
- telex: `soos` → `số`;
- tên trong dữ liệu shop: khớp, sửa dấu, bảo vệ khỏi các bước sau;
- lexicon: `dthu` → `doanh thu`, `mô` → `nào`, `đh` → `đơn hàng`. Các mục viết trong code đều có trong câu hỏi train.
  [assets/lexicon_external.json](../assets/lexicon_external.json) có 85 mục: 67 mục do Gemini sinh từ từ gốc của train
  (lúc phát triển, lúc chạy không gọi API; phản hồi thô giữ trong `assets/lexicon_gemini_*.jsonl`), 16 mục từ trang web
  (có URL), 2 mục viết tay (`đh`, `dh`). Đây là dữ liệu sinh duy nhất, được khai báo trong README của image và
  `training_code/`;
- số tiền, ngày: `3tr7` → `3.700.000 đồng`, `th7/26` → `tháng 7/2026`.

**LLM rewrite** (`--rewrite residual`): chỉ gọi khi frame còn từ chưa giải thích. Gọi một lần, thinking off, tối đa 400 token.
Bản viết lại bị loại nếu làm mất một con số hoặc một tên khớp chính xác.

### C3. Code đọc câu hỏi

| thành phần | dùng để |
|---|---|
| `expect_answer` | kiểu đáp án mong đợi: mẫu chính xác ≥ 99% trên train là `hard` |
| `values.hints` | cụm từ khớp giá trị cột (`tiền mặt` → `"Trả bằng" = 'cash'`) |
| `subjective_note` | ghi chú khi câu hỏi mang tính chủ quan |
| `pick_rules` | 5 dòng rulebook trùng từ nhiều nhất với câu hỏi |
| `bank.query` / `bank.nearest` | 4 ví dụ train gần nhất của shop khác / độ giống, dùng cho router |
| `time_hints` | giải kỳ thời gian theo rulebook §2 ra ngày cụ thể |
| `build_frame` | khung: đại lượng, macro, kỳ, tên, giá trị, dạng đáp án, khả năng cần, từ dư |
| `shadow` | (câu hỏi, từ, đến) khi câu chỉ có một kỳ: macro dùng để kiểm tra chéo |

### C4. Dựng prompt

```
system:  SYSTEM_TEMPLATE SQL (+ dòng kế hoạch) + RULEBOOK của split + định nghĩa đã tắt
user:    [biz_part]       README + schema card (views, raw, Keys, ghi chú slots/WARNING, macro, capabilities)
         [question_part]  4 ví dụ của shop khác (Q, SQL, A); as_of + thứ; câu gốc và bản chuẩn hóa; kiểu mong đợi;
                          tên, số tiền, giá trị; dòng rulebook liên quan; frame; kỳ thời gian đã giải
```

Prompt dài khoảng 13–16k token.

### C5. Router thinking (`route_reasons`)

Câu vào **tầng deep** (thinking on, `16384` token) nếu có một trong các lý do:
- "changed rule";
- "suspect sales";
- "unfamiliar": độ giống với câu train gần nhất < 0.75;
- "suspect <khả năng>";
- "residual N" với N ≥ 3.

Không có lý do nào thì câu ở **tầng normal**: thinking off, 2048 token.

### C6. LLM viết SQL, chạy, kiểm tra, sửa (`_samples`)

Một mẫu greedy (temperature 0, seed 0):

1. **Gọi LLM** qua API OpenAI-compatible của vLLM. Lỗi kết nối được thử lại với backoff; nếu vẫn lỗi thì cả lượt dừng
   (`LLMUnavailable`) để lượt khởi động lại trả lời các câu còn lại.
2. **`extract_sql`**: lấy khối ```sql hoặc dòng `UNANSWERABLE:`.
3. **Lời khuyên lượt đầu**:
   - `term_advice`: thuật ngữ đã có macro kiểm thử mà truy vấn tự dựng lại;
   - `frame_advice`: ca vs doanh thu ca, thiếu `pct_change`, `count(*)` trên dòng để đếm đơn, kỳ SQL lệch kỳ của frame…

   Các lời khuyên này yêu cầu viết lại một lần, không chặn.
4. **`execute`** ([sql/agent.py](../src/datamate/sql/agent.py)):
   - **kiểm tra trước khi chạy:** thiếu dòng `-- type:`, `LIMIT` không có `ORDER BY`, hoặc gọi macro bị chặn thì báo lỗi;
   - **viết lại tự động:** sửa cách gọi macro, thêm điều kiện bỏ khách lẻ, bỏ dòng hàng không tên;
   - **lint:** sai độ hạt, tiền cấp đơn cộng trên từng dòng;
   - **chạy:** đặt `as_of`, chạy SELECT với timeout 30 s, thử nối lại khóa khi báo thiếu cột;
   - **ghi lại** `tables` (bảng store mà truy vấn quét) và `reads` (biểu thức của kế hoạch), cả hai lấy từ `EXPLAIN`: view và
     macro đều đã được mở ra, nên biết cả cột mà macro đọc. Một `count(*)` trơn được đọc từ thống kê bảng nên kế hoạch không có
     bảng nào; khi đó coi là "không biết";
   - **khi lỗi Binder/Catalog**, thông báo gửi mô hình có thêm `schema_hint`:
     - cột không tồn tại: mọi cột của các bảng trong FROM;
     - bảng không tồn tại: danh sách bảng và view;
     - macro không tồn tại: danh sách macro, trừ macro bị chặn;
   - `to_answer`, `check_result` (tên có trong dữ liệu, kết quả 0 hay rỗng, kỳ ngoài dữ liệu), và kiểm tra chéo bằng macro
     (shadow).
5. **Hậu kiểm**: `fit_month`, `fit_money`, `check_answer`, `invariants`.
6. **Sửa**: có lỗi thì gửi `sql_repair_message`; có cảnh báo thì gửi `sql_check_message`. Tối đa 1 lượt sửa.
   - **Thinking bị cắt** (câu trả lời rỗng, `finish_reason = length`): lượt cắt không tính. Lượt sau chạy với thinking off và
     **vẫn có lượt sửa của nó**. Trên public_e, 31/43 câu "không có kết quả" rơi vào trường hợp này.
7. **Kết thúc mẫu**: cảnh báo cứng còn lại sau lượt sửa thì bỏ kết quả (`unconfirmed`). Không có kết quả mà macro đã cho
   `contract` thì dùng `contract`.

### C7. Vote

Với 1 mẫu: có kết quả thì giữ, không có thì `abstain`. **Rào đón** (`doubt`): mô hình trả lời nhưng cũng viết UNANSWERABLE.

### C8. Leo thang

Câu ở tầng normal được làm lại toàn bộ ở tầng deep nếu kết quả đầu là `abstain`, `unanswerable`, có rào đón, có cảnh báo kiểm
tra, hoặc bị cắt vì `length`.

### C9. Judge ([agent/judge.py](../src/datamate/agent/judge.py))

Chỉ chạy ở tầng deep hoặc escalated, với UNANSWERABLE hoặc câu trả lời có rào đón:

| solver | judge | kết quả |
|---|---|---|
| trùng ý | | giữ |
| UNANSWERABLE | dữ liệu có | solver thử thêm 1 lần theo gợi ý; không ra đáp án hợp lệ thì `abstain` |
| trả lời, có rào đón | dữ liệu thiếu | `unanswerable` |
| trả lời, không rào đón | dữ liệu thiếu | `abstain` |

### C10. Cổng quyết định ([agent/gates.py](../src/datamate/agent/gates.py))

Các luật xác định, không gọi mô hình. Mỗi cổng đã được đo trên lượt chạy train trước khi bật. Cổng đầu tiên khớp sẽ quyết định:

| cổng | điều kiện | kết quả |
|---|---|---|
| `shop_suspect` | shop `suspect`, kiểu đáp án là number/percent/string/list/table, **và truy vấn quyết định chạm phạm vi của cờ**: bảng đã quét thuộc phạm vi, hoặc tên cột phạm vi xuất hiện trong SQL hay trong kế hoạch | abstain |
| `unans_in_sql` | đang abstain, dòng UNANSWERABLE nằm trong khối SQL, judge không chọn abstain | unanswerable |
| `count_money` | hỏi "bao nhiêu đơn/khách/ly..." mà đáp án ≥ 1.000.000 | abstain |
| `items_len` | hỏi "bao nhiêu món" mà đáp án là list | number = số phần tử |
| `table_words` | đáp án table mà câu không có từ chỉ bảng | abstain |
| `expect_type` | kiểu `hard` mong đợi khác kiểu đáp án | abstain |

Về `shop_suspect`:
- Truy vấn quyết định là lượt thử lại của judge nếu judge trả lời, nếu không thì lượt cuối có kết quả.
- Nếu không biết truy vấn đọc gì (không có kế hoạch, hoặc đáp án đến từ `contract`), cổng chặn như trước.
- Trước bản cuối, cổng chặn cả shop: trên public_e nó bỏ 73 câu đúng và 12 câu sai, mất 0.061.

Log ghi `gated = {gate, from}`.

### C11. Finalize và ghi

`finalize` ([postprocess.py](../src/datamate/postprocess.py)):
- number là số JSON;
- percent làm tròn 2 chữ số;
- date dạng `YYYY-MM-DD` hoặc `YYYY-MM`;
- list không rỗng;
- table có `columns` và `rows`.

Sai định dạng thì thành `abstain`.

---

## 6. Output

Mỗi câu được ghi ngay khi xong, có khóa và `flush`, nên nếu bị dừng ở giới hạn giờ thì các câu đã xong vẫn còn.

| file | nội dung |
|---|---|
| `submission.jsonl` | `{"id", "type", "answer"}`, `{"id", "type": "unanswerable", "reason"}` hoặc `{"id", "type": "abstain"}` |
| `logs/<id>.json` | đầu file là các trường guideline yêu cầu: `id`, `answer`, `elapsed_ms`, `thinking`, `code`, `output`. Sau đó là log đầy đủ: `pre`, `expect`, `examples`, `hints`, `samples[].attempts[]` (reply, code, ran, result, error, warning, tables, reads, thinking, finish_reason), `vote`, `route`, `judge`, `frame`, `audit`, `gated`, `slots`, `elapsed` |
| `config.json` | RunConfig cùng semantic, ingest và store version |
| `capabilities.json`, `audit.json`, `slots.json` | trạng thái khả năng; cờ audit kèm phạm vi; slots đã áp dụng của từng shop |
| `predict.log`, `vllm.log`, `part<i>.log` | log của predict.sh |

Đo lại cổng trên một lượt đã chạy: `uv run datamate gate <run_dir> --gold <answers.jsonl>`.

---

## 7. Số lời gọi LLM

| thời điểm | lời gọi | thinking | token tối đa |
|---|---|---|---|
| mỗi split | `rule_gate`, `rule_delta` (cache) | off | 400 |
| mỗi shop | `resolve_columns`, `resolve_statuses` (nếu cần, cache) | off | 400 |
| mỗi shop | slots: 1 lời gọi cho mỗi slot kích hoạt (cache) | on | `16384` |
| mỗi câu, tiền xử lý | rewrite: 0 hoặc 1 | off | 400 |
| mỗi câu, normal | solver: 1–2 | off | 2048 |
| mỗi câu, deep / leo thang | solver: 1–3 (lượt bị cắt + lượt không thinking + lượt sửa) | on | `16384` |
| mỗi câu, judge | 1, thêm 1 lượt solver nếu judge thấy dữ liệu có | on | `16384` |

Thời gian: trên 1× RTX 5090 với thinking 16384, public_i (1000 câu, 4 nhóm song song) mất khoảng 48 phút, gồm prepare 144 s
và nhóm chậm nhất 45 phút. Với thinking 8192 thì mất khoảng 25–35 phút. Cấu hình nộp dùng 2× 5090 và có 6 giờ.

---

## 8. Kiểm chứng và tuân thủ

Quy tắc từ QnA của BTC ([description/QnA.txt](../description/QnA.txt)): code suy luận không được chứa gì học từ public test,
gồm câu chữ rulebook, macro, header, bố cục, hay bản sửa chọn theo nhãn public. Một bản sửa chỉ được thêm khi train có trường
hợp đó. Riêng tiền xử lý câu hỏi được miễn, theo lựa chọn của người dùng.

| thay đổi | kiểm chứng | cơ sở |
|---|---|---|
| slots 0–3 | train: không slot nào kích hoạt; mutate train với gold: `rename_order_key` 826, `status_in_notes` 802, `abbrev_names` 826, `amount_before_discount` 826 (baseline 826/859) | kích hoạt theo khoảng trống dữ liệu, LLM đọc README và rulebook của split lúc chạy |
| `shop_suspect` theo phạm vi | phát lại trên public_e: +58; trên train không đổi (không shop nào bị gắn cờ) | người dùng chọn; thiết kế: cờ trên một bảng không làm sai câu đọc bảng khác |
| phạm vi `fill_gap` gồm mọi bảng điền từ dòng đầu | public_g: +3 | sửa lỗi của chính bản thu hẹp |
| lượt sửa sau khi thinking bị cắt | unit test | trên train route run có 8 trường hợp cùng loại |
| `schema_hint` khi lỗi Binder/Catalog | unit test | lỗi SQL chung, không gắn với dữ liệu nào |
| `customer_group` không lấy từ danh mục sản phẩm | train: `sql-check` 826/859 không đổi | sửa lỗi logic (thuộc tính của sản phẩm không phải nhóm của khách); train không có trường hợp lỗi, người dùng chấp nhận |
| slot `unit_factor`, cờ `unit_gap`, `WARNING` | train: không kích hoạt (30 shop); mutate `factors_in_text`, `rename_base_qty` (8 shop): áp dụng trên 6, từ chối an toàn trên 2 (biz_121 quy đổi lồng hộp → vỉ → viên, biz_122 khoảng trống chỉ giảm 6%), hợp đồng doanh thu/số đơn giữ nguyên 247/262 khớp chính xác, không miss; phát lại public: 9/10 câu kiểm chứng khớp gold | người dùng chấp nhận ("có, hãy thử"); kích hoạt theo khoảng trống dữ liệu, LLM đọc README và danh mục |
| `unit_gap` không làm shop `suspect` | public_i: cổng chặn 17 câu đúng, 7 câu sai trên dev_004, dev_013, nơi slot đã quy đổi phần lớn và chỉ còn vài đơn vị thiếu hệ số (dev_004: 255 dòng bán theo tấn); phát lại: 0.789 → 0.799; train không đổi (không shop nào bật cờ) | **chọn theo nhãn public**, người dùng quyết định; cờ vẫn ghi trong audit, `WARNING` vẫn có trong card |
| ngân sách thinking 16384 (vLLM `--max-model-len 49152`) | public_h: 60/60 đối chứng giữ đúng; public_i 0.789 so với public_g 0.775 (thinking 8192), lẫn với các thay đổi khác | cấu hình |

---

## 9. Lỗi còn lại

Phân tích trên public_g bằng cách chạy lại SQL của model trên store với một chỗ sửa có kiểm soát. Không dùng lời giải thích sau
của model.

**Do model (store đúng):**
- chọn sai phép gộp dù bộ lọc đúng: `sum(amount)` cho câu "bao nhiêu máy"; `revenue_of` cho giá trị trung bình đơn;
- `AND … OR …` thiếu ngoặc;
- quên lọc nhóm (bỉm size M lẫn với núm bình sữa size M);
- đếm số dòng giao hàng thay vì số chuyến;
- đọc sai tiếng lóng: "10 củ", "cất hàng", "Ca th8" (= cả tháng 8);
- trả lời câu chủ quan ("nhà cung cấp uy tín").

**Do dữ liệu, cố ý chưa sửa** (chỉ thấy trên public, train không có, nên sửa sẽ trái quy tắc):

| shop | lỗi | ≈ câu |
|---|---|---|
| dev_010, dev_003 | header nhóm khách `Loại khách` / `Kiểu khách` | 6 |
| dev_008 | nợ ghi dạng chữ trong cột Note | 4 |
| dev_002 | topping ghi chung ô với món | 4 |
| dev_012 | phiếu sửa xe: hãng xe và biển số chung một ô, số xe khác nhau, hạng mục trong chữ | ~13 |
| dev_014 | trả góp: hợp đồng theo khối, kỳ hạn | 7, đã bị `shop_suspect` chặn |

**Gold ở máy thiếu alias:** biệt danh thợ (dev_012), tên khách kèm số điện thoại (dev_008). Khoảng 6 câu, có thể đúng trên
leaderboard.

---

## 10. Bảng tra: bước → file

| bước | file chính |
|---|---|
| điểm vào Docker | [docker/predict.sh](../docker/predict.sh), [docker/Dockerfile](../docker/Dockerfile) |
| CLI | [cli.py](../src/datamate/cli.py) (`run`, `slots`, `gate`, `audit`, `score`, `sql-check`, `facts-check`...) |
| điều phối | [agent/pipeline.py](../src/datamate/agent/pipeline.py) |
| rulebook → cổng định nghĩa | [rules.py](../src/datamate/rules.py) |
| ingest | [ingest/](../src/datamate/ingest/) |
| vai trò cột / trạng thái bằng LLM | [agent/columns.py](../src/datamate/agent/columns.py), [agent/statuses.py](../src/datamate/agent/statuses.py) |
| slots | [slots/base.py](../src/datamate/slots/base.py), [slots/apply.py](../src/datamate/slots/apply.py) |
| sales_lines, facts, trust, capabilities, audit | [canon.py](../src/datamate/canon.py), [facts.py](../src/datamate/facts.py), [ingest/trust.py](../src/datamate/ingest/trust.py), [capabilities.py](../src/datamate/capabilities.py), [audit.py](../src/datamate/audit.py) |
| DuckDB store, macro, kế hoạch truy vấn | [sql/store.py](../src/datamate/sql/store.py), [sql/dims.py](../src/datamate/sql/dims.py) |
| tiền xử lý | [preprocess/](../src/datamate/preprocess/) |
| kỳ thời gian, frame, ví dụ, dòng rulebook | [agent/hints.py](../src/datamate/agent/hints.py), [agent/frame.py](../src/datamate/agent/frame.py), [agent/retrieve.py](../src/datamate/agent/retrieve.py), [agent/evidence.py](../src/datamate/agent/evidence.py) |
| prompt SQL, card, chạy truy vấn, lint, gợi ý schema | [sql/agent.py](../src/datamate/sql/agent.py), [agent/prompt.py](../src/datamate/agent/prompt.py), [agent/lint.py](../src/datamate/agent/lint.py) |
| client LLM | [agent/llm_local.py](../src/datamate/agent/llm_local.py) |
| kiểm tra đáp án, finalize | [postprocess.py](../src/datamate/postprocess.py) |
| vote, judge, gates | [agent/vote.py](../src/datamate/agent/vote.py), [agent/judge.py](../src/datamate/agent/judge.py), [agent/gates.py](../src/datamate/agent/gates.py) |
| mutate train (kiểm chứng slots) | [eval/mutate.py](../src/datamate/eval/mutate.py) |
| chạy thử nghiệm trên GPU thuê | [remote/setup_server.sh](../remote/setup_server.sh), [remote/serve_vllm.sh](../remote/serve_vllm.sh), [remote/run_parallel.sh](../remote/run_parallel.sh) |

---

## 11. Có trong code nhưng không chạy ở cấu hình nộp

| tính năng | cờ | lý do tắt |
|---|---|---|
| biên dịch macro cho luật mới/đổi | `--compile-delta` | public: 4/176 câu gọi tới, không câu nào đúng, tốn 21–28 phút prepare |
| calibrator logistic | `--calibrator` | fit trên train (leave-business-out): ngưỡng 0.5 không loại câu nào |
| fast branch | `--fast` | không dùng |
| shop views do mô hình viết | `--shop-views` | không dùng |
| nhiều mẫu + vote | `--samples N` | tốn thời gian; 1 mẫu greedy giúp tái lập kết quả |
| catalog `primitives` / `compiled` | `--catalog` | thử nghiệm |
| engine pandas | `--engine pandas` | dừng từ 2026-11-08 |
| backend Claude / Gemini | `--backend` | chỉ dùng trên train |
| shop gate cũ dựa trên tỉ lệ | `shop-gate-fit`, `shop-gate` | thay bằng audit + cổng `shop_suspect` |
