# Kế hoạch: nhánh nhanh + Text-to-SQL trên DuckDB

**Ngày:** 2026-11-05, sau góp ý của mentor.
**Trạng thái:** kế hoạch, chưa cài đặt. Các con số dưới đây đo trên dữ liệu thật, trừ khi ghi là mục tiêu.

## 0. Phạm vi

| hạng mục | quyết định |
|---|---|
| Text-to-SQL trên DuckDB | **làm**, là hướng thử chính |
| Nhánh nhanh, rule theo business, biến thể tên, data mới, primitive | **làm** |
| LLM sinh code pandas | **tạm dừng**, giữ nguyên để so sánh và làm phương án dự phòng (README, mục Status) |
| Docker, vLLM, Blackwell | **hoãn**, giữ 13–15/11 cho phần này |
| Nhãn cho public test | **tạm dừng**: chưa cần kết quả public; có thể tự chạy model trên Vast AI nếu rẻ hơn |

**Ngân sách thời gian.** Số câu private chưa được công bố. Nhóm giả định 500 câu; nếu bằng public thì là 1000.

| số câu private | giây cho mỗi câu (throughput, 6 giờ) |
|---|---|
| 500 | 43 |
| 1000 | 21,6 |

Thiết kế theo mốc **1000 câu** để an toàn. Đây là throughput, không phải độ trễ: các câu chạy song song.

## 1. Kiến trúc đích

```
 File thô ──► Ingest ──► bảng sạch ──► Kiểm định mapping ──► DuckDB của shop
                                       (trusted/suspect/absent)  views + macros + dim tên
                                                                     │
 Rulebook ──► Rule registry (mệnh đề, ngành, trạng thái) ────────────┤
                                                                     ▼
 Câu hỏi ──► Chuẩn hoá ──► Frame parser ──► Router
                           (metric, kỳ, lọc, phép toán)
                                              │
                ┌─────────────────────────────┼───────────────────────┐
                ▼                             ▼                       ▼
        Nhánh nhanh (không LLM)        Nhánh SQL (LLM)         Unanswerable tất định
        macro đã kiểm thử              SQL trên views          thiếu trường, kỳ ngoài dữ liệu
                └─────────────┬───────────────┘                       │
                              ▼                                       │
                Kiểm tra kết quả + finalize ◄─────────────────────────┘
                              ▼
                 đáp án │ unanswerable │ abstain
```

W1–W5 dưới đây **không phụ thuộc SQL**. Nếu cuối cùng quay lại sinh code, các phần này vẫn dùng được, chỉ cần gọi chúng từ Python.

## 2. DuckDB và Text-to-SQL

DuckDB không phải phương án thay thế Text-to-SQL. **DuckDB là engine chạy SQL, Text-to-SQL là cách LLM sinh truy vấn.** Bảng sạch và mô tả trường hiện có được nạp vào DuckDB, rồi LLM viết SQL trên đó.

**Đã kiểm chứng (DuckDB 2.0.0, `datamate sql-check`):**

| kiểm tra | kết quả |
|---|---|
| Tính 5 đại lượng contract bằng macro trên train | **766/766 khớp đáp án** trong số tính được (doanh thu 259, trung bình đơn 167, số đơn 136, số huỷ 100, tỉ lệ huỷ 104). Bỏ qua 58 câu: 33 có nhiều kỳ, 25 ở shop không có `sales_lines` |
| Dựng DB cho 44 shop (train + public) | không shop nào lỗi; 308 MB tổng cộng |
| Chạy truy vấn bằng SQL trên 733 câu (thử nghiệm đầu) | 0,48 s tổng cộng |
| Cắt theo `as_of` trong view bằng `getvariable('as_of')` | chạy đúng trên file read-only |
| Macro vô hướng và macro dạng bảng | chạy đúng |
| Chặn đọc file ngoài, ghi, đổi cấu hình | bị chặn |
| Timeout bằng `interrupt()` | dừng sau 0,31 s |
| Sai tên cột | báo `Candidate bindings: "Nhóm hàng"` |

**Vấn đề đã phát hiện:**
- `round_half_up` viết bằng `power()` sai ở `1.005`. **Đã sửa:** macro dùng DECIMAL và có test so với `dm.round_half_up`.
- 3 shop không có mã đơn (`biz_107`, `biz_113`, `biz_126`) không có bảng `orders`. Hiện các shop này chỉ có macro `revenue`; còn phải xử lý theo rulebook §8 (mỗi dòng là một lần ghi).

**So với sinh pandas:**

| | Text-to-SQL trên DuckDB | sinh pandas |
|---|---|---|
| Lọc `as_of` | nằm trong view, LLM không thể quên | LLM phải gọi `in_period` |
| Lỗi kiểu, lỗi API (E0: TypeError 70, ValueError 29, IndexError 17, AttributeError 9) | phần lớn nên giảm | còn nguyên |
| Sai tên cột (E0: KeyError 186) | vẫn xảy ra, nhưng phát hiện trước khi chạy và có gợi ý tên đúng | phát hiện lúc chạy |
| Lỗi định nghĩa nghiệp vụ | views giải quyết; views cũng làm được ở pandas | như bên trái |
| Sandbox | có sẵn, nhẹ | tiến trình riêng, tốn RAM |
| Logic nhiều bước (công nợ, tồn tại ngày, hiệu ứng tăng giá) | phải đưa vào macro, nếu không SQL dài và dễ sai | linh hoạt hơn |
| Tổng quát với ngành mới | kém hơn nếu logic không nằm gọn trong SQL | tốt hơn |

**Kỳ vọng:** SQL giảm lỗi thực thi, nhưng không tự sửa lỗi hiểu sai câu hỏi. Vì vậy cần cổng G1 ở mục 5 thay vì giả định SQL tốt hơn.

## 3. Số liệu dẫn đến ưu tiên

E0 chạy code cũ hơn `98d4c7b`, nên bảng này chỉ dùng để xếp ưu tiên. Nhóm intent được gán bằng regex, chỉ là ước lượng. "Điểm mất" = số câu − đúng + sai.

| intent | train | PA E0 | điểm mất | tỉ trọng public |
|---|---|---|---|---|
| doanh thu | 1204 | 0,75 | 297 | 37,1% |
| trung bình | 192 | −0,17 | 224 | 4,5% |
| công nợ | 117 | −0,50 | 176 | 5,1% |
| khách | 178 | 0,07 | 165 | 11,0% |
| số đơn | 177 | 0,12 | 156 | 8,2% |
| xếp hạng sản phẩm | 160 | 0,35 | 104 | 3,5% |
| số lượng theo đơn vị | 82 | 0,15 | 70 | 2,1% |
| doanh thu theo nhóm | 115 | 0,57 | 49 | 1,3% |
| hiệu ứng giá | 106 | 0,55 | 48 | 1,5% |
| ca, nhân viên | 92 | 0,63 | 34 | 3,6% |
| nhập hàng | 72 | 0,56 | 32 | 1,1% |
| giao hàng, phiếu, bảo hành | 18 | −0,61 | 29 | 4,4% |
| lô, hạn dùng | 22 | −0,09 | 24 | 1,2% |
| tồn kho | 44 | 0,50 | 22 | 0,4% |
| lợi nhuận | 18 | −0,11 | 20 | 3,1% |

Các nhóm giao hàng, phiếu và lợi nhuận có ít câu ở train nhưng nhiều hơn hẳn ở public, nên không thể xếp hạng chỉ theo train.

## 4. Các hạng mục

### W0. Bộ đánh giá

- Chạy lại baseline pandas trên code hiện tại, vì E0 chưa phản ánh `98d4c7b`.
- Silver label cho public theo mục 6 đang **tạm dừng**. Hiện chỉ đánh giá trên train.

### W1. Biến thể tên

**Quan sát:**
- Có 441 nhóm biến thể tên hàng (25 shop) và 863 nhóm biến thể tên khách (20 shop).
- Biến thể chỉ khác hoa thường, dấu cách, mất dấu, hoặc `330ml`/`330 ml`.
- Ở `biz_109`, *Sữa chua Vinamilk có đường* có 3 cách viết (1098 + 245 + 220 dòng).
- Trùng tên khác mã: 6 trường hợp ở khách, 0 ở hàng.

**Đã làm** ([src/datamate/sql/dims.py](../src/datamate/sql/dims.py)):
- `dim_product` và `dim_customer`: `entity_key`, `code`, `display_name`, `aliases`, `n_lines`.
- Khoá gộp: **có mã thì theo mã**; không có mã thì theo tên, bỏ khác biệt hoa thường, dấu câu, khoảng trắng và khoảng cách trước đơn vị.
- Bỏ dấu không đủ để coi là cùng tên: `Doãn Dũng Thanh` và `Đoàn Dũng Thanh` cùng thành `doan dung thanh` nhưng là hai người. Hai từ khác nhau chỉ được gộp khi một bên không dấu. Dạng nhiều dấu nhất lập cụm trước, để dạng không dấu không nối hai cụm khác nhau.
- **Không dùng fuzzy merge** trong dữ liệu. Rulebook §3 quy định hai mã trùng tên là hai khách, hai tên khác chữ là hai khách.
- `display_name` là cách viết phổ biến nhất. Bộ chấm nhận mọi cách viết có trong file.
- `build_sales` có thêm `product_key`, `customer_key`; `product_name`, `customer_name` chỉ còn một cách viết cho mỗi đối tượng, cách viết gốc ở `*_name_raw`. Nhờ vậy cả pandas và SQL đều dùng được mà không cần sửa prompt.

**Còn lại:**
- Dạng không dấu khớp nhiều cụm sẽ gắn vào cụm phổ biến nhất; đây là lựa chọn tất định, không chắc đúng.
- Fuzzy chỉ dùng ở phía câu hỏi, để nối câu với `aliases`. Nhiều ứng viên ngang điểm thì không vào nhánh nhanh.
- Mở rộng thêm: tách topping chung ô (`dev_002`), sheet `Ghi tắt` (điện thoại), biệt danh thợ và dòng xe (sửa xe).

**Đã đo** (`gemini-3-flash-preview`, 60 câu train):

| | đúng / sai / bỏ qua | PA |
|---|---|---|
| trước | 54 / 5 / 1 | 0,82 |
| sau | **58 / 1 / 1** | **0,95** |

ΔPA +0,13, khoảng tin cậy 95% là [0,02; 0,28]. Số này có thể lạc quan: chính 60 câu này đã giúp phát hiện lỗi, và chưa đo trên Qwen3.5-9B. Hai thay đổi đúng ↔ bỏ qua giữa hai lần chạy nhiều khả năng là nhiễu của model, không phải do sửa tên.

### W2. Data mới: không map được hoặc map sai

Nguyên tắc: **kiểm định bằng giá trị, không tin tên cột.**

**Bước 1: chấm từng vai trò cột**

| vai trò | điều kiện kiểm tra |
|---|---|
| ngày | ≥ 95% parse được; nằm trong khoảng ngày của README |
| tiền | ≥ 95% là số, không âm trừ dòng trả |
| mã đơn | có lặp ở bảng dòng hàng, không rỗng sau forward-fill |
| trạng thái | ít giá trị khác nhau; từ vựng có thể chuẩn hoá về 4 nhãn |
| số lượng | số dương, phần lớn nhỏ |

**Bước 2: đối chiếu chéo**
- `qty × giá − giảm ≈ thành tiền`.
- Tổng dòng ≈ tổng đơn, sau khi tính giảm giá và phí ship.
- Mã trên dòng tìm thấy trong danh mục, tỉ lệ khớp cao.
- Ngày cuối dữ liệu khớp "Dữ liệu có đến hết" trong README.

**Bước 3: gán trạng thái cho từng năng lực** (doanh thu, khách, nợ, tồn, nhập, lô, nhân viên...)

| trạng thái | điều kiện | hành vi |
|---|---|---|
| `trusted` | đủ vai trò, qua các kiểm tra | nhánh nhanh và SQL |
| `suspect` | có dữ liệu nhưng có kiểm tra thất bại | tắt nhánh nhanh; SQL kèm cảnh báo; ngưỡng abstain cao hơn |
| `absent` | cả bảng gốc cũng không có trường liên quan | unanswerable tất định |

**Đã làm trước (2026-11-06, `INGEST_VERSION` 12):**
- `_subtotal_rows` bỏ đúng dòng tổng có ô ngày trống (`pd.NaT` từng bị tính là có ngày): 587 dòng ở `dev_008`, 2 dòng ở `biz_105`.
- Ingest cảnh báo khi bảng còn dòng không có mã đơn sau forward-fill. Hiện 0 cảnh báo trên 44 shop.
- `busy_customers` phát hiện nhãn khách lẻ chưa biết bằng tần suất: ≥ 5% số đơn và ≥ 20 lần trung vị (khách thật tối đa 0,64% và 3 lần). Có từ gợi ý thì coi là khách lẻ, không có thì chỉ ghi chú cho model. Không mở rộng danh sách từ, vì mẫu rộng hơn bắt nhầm tên thật như `Lê Hiếu Ngân`. Ngưỡng đo trên train và public, chưa kiểm chứng ở ngành có khách lớn thật.

**Trạng thái lạ (thêm 2026-11-06):** `normalize_status` hiện trả `hoàn thành` cho mọi giá trị không khớp, nên `Không nhận hàng`, `Bom hàng`, `Chờ xác nhận` sẽ âm thầm thành doanh thu. Sửa: danh sách đóng cho `hoàn thành`; giá trị lạ được LLM phân loại **một lần cho mỗi giá trị** lúc ingest, về đúng 4 nhãn của rulebook (không dùng taxonomy riêng, vì rulebook quy định "đúng một trong bốn trạng thái"). Không chắc thì đánh dấu `suspect`, không mặc định là hoàn thành.

**Vai trò gán nhầm đã thấy (quét 44 shop, 2026-11-06):** tình trạng máy ở shop điện thoại (`pin yếu`, `móp cạnh`, `đẹp 99%`) bị gán vai trò `status`; `đang giao dịch` thành `đang giao`. Chưa đo tác động đến đáp án.

**Xử lý khi không map được:**
1. Giữ mọi cột không có vai trò dưới dạng bảng `raw_*` kèm mô tả từ README, để SQL vẫn truy vấn được.
2. Gọi LLM **một lần cho mỗi bảng** lúc ingest: đầu vào gồm tiêu đề, 5 giá trị mẫu, mô tả README; đầu ra là vai trò dạng JSON. Greedy, cache lại, không tốn thời gian theo câu.
3. Mọi vai trò do LLM gán phải qua bước 1–2, nếu không sẽ bị bỏ.
4. Thêm khái niệm **bảng giao dịch tổng quát** (mỗi dòng một lần ghi) cho shop không có cấu trúc đơn + dòng. Áp dụng cho `dev_012`, `dev_009`, `biz_110` và 3 shop không có mã đơn.

`absent` phải xét trên **cả bảng gốc**, không chỉ vai trò. `dev_012` chỉ gán được vai trò cho 1/13 cột, nhưng thiếu vai trò không có nghĩa là thiếu dữ liệu.

**Kiểm thử bằng đột biến:** đổi tên cột, xáo cột và đổi định dạng trên shop train, rồi đo tỉ lệ khôi phục vai trò và tỉ lệ giữ đúng 733 câu contract. Việc này mô phỏng "shop chưa gặp" mà vẫn có đáp án thật. Bộ đột biến làm **trước** các phần AI và gồm cả trạng thái lạ, tên biến thể, dòng tổng, đổi định nghĩa rulebook. Tách riêng thay đổi giá trị với thay đổi luật, vì hai loại này đo hai năng lực khác nhau.

### W3. Rule theo business

**Quan sát (train so với public):** 67 mệnh đề chung, 64 giống hệt, 3 đổi chữ; train có thêm 3, public có thêm 24. Ba mệnh đề đổi đều là **làm rõ thêm**, không đổi định nghĩa cũ. Nếu so hash nghiêm ngặt, chỉ riêng "Đơn thuộc kỳ nào" đã tắt nhầm các macro doanh thu.

**Thiết kế:**
1. **Phân tích rulebook** thành các mệnh đề: `(mục, khoá, nội dung, ngành)`.
2. **Xác định ngành** từ schema và README, không dùng ID shop. Có thể thuộc nhiều ngành hoặc không ngành nào.
3. **Áp theo thứ tự ưu tiên:** câu hỏi > README của shop > luật ngành > luật chung. Rulebook nêu rõ câu hỏi đứng trên luật mặc định và luật ngành đứng trên luật chung; vị trí của README là quyết định của nhóm, vì README mô tả cách shop này ghi dữ liệu.
4. **Mỗi macro khai báo mệnh đề nó phụ thuộc,** kèm các phiên bản văn bản đã kiểm thử.
5. **Khi tải rulebook:**

| trạng thái mệnh đề | hành vi |
|---|---|
| giống phiên bản đã kiểm thử | bật macro |
| khác | hỏi LLM **một lần cho mỗi mệnh đề, mỗi bộ dữ liệu**: nghĩa có đổi không? Chỉ khi chắc chắn không đổi mới bật |
| đổi nghĩa, không còn, hoặc LLM không chắc | tắt ở nhánh nhanh; SQL đọc văn bản mới |

Trả lời câu hỏi "có nên mặc định rule giống nhau không": **không**. Coi rule giống nhau chỉ khi đã kiểm chứng; mặc định khi chưa rõ là đi nhánh SQL có đọc rulebook.

**Đo:** đưa vào các biến thể rulebook tổng hợp (ví dụ: tính cả đơn có hàng trả). Macro phụ thuộc phải tắt 100%.

### W4. Primitive thành macro

Mentor đúng ở điểm này. Macro là "tool": nhánh nhanh gọi trực tiếp, LLM gọi trong SQL. Thứ tự theo điểm mất và tỉ trọng public:

| đợt | macro hoặc view | mệnh đề |
|---|---|---|
| 1 | `revenue`, `order_count`, `cancel_count`, `cancel_rate`, `avg_order_value` (chuyển từ `dm`) | §1, §3 |
| 1 | `customers_in(a,b)`, `customer_sales`, `customer_order_count`, khách trung thành, khách sộp, hay đổi ý | §3 |
| 1 | `debt_at(d)`, `debt_by_customer(d)`, `debt_aging(d)`, `debt_flow(a,b)` | §4 |
| 2 | `qty_base_sold(a,b)`, `sold_in_unit(unit,a,b)`, `product_rank`, ưa chuộng nhất | §3, §10 |
| 2 | `gross_profit(a,b)`, `net_profit(month)` | §5 |
| 2 | `stock_at(d)`, `stock_value(d)`, `out_of_stock(d)`, `slow_movers(a,b)` | §2, §5 |
| 3 | `import_total`, `main_supplier`, `price_on`, `price_increase_effect` | §5 |
| 3 | `expiring_lots(d,n)`, `nearest_lot(p)`, tỉ lệ kê đơn, doanh thu hoạt chất | §6 |
| 3 | sửa xe: lượt xe, tỉ lệ bảo hành, xe khách quen; điện thoại: trả góp, thu máy cũ, dòng máy, hãng; mẹ và bé: voucher, size | §6 |
| luôn có | `round_half_up`, các hàm kỳ, `pct_change`, hoà theo mã | §2, §9 |

Mỗi macro: một định nghĩa, gắn mệnh đề, có test với đáp án train. Ngành không có ở train (sửa xe, điện thoại, mẹ và bé) chỉ kiểm bằng silver label.

### W5. Nghiên cứu nhánh nhanh

**Câu hỏi nghiên cứu:** bao nhiêu phần trăm câu có thể trả lời không cần LLM với precision ≥ 98%, và kết quả có giữ được trên shop và ngành chưa gặp không?

**Cơ sở:**
- Các contract hiện có phủ 25,6% train và 15,3% public.
- Trên train: 766 đúng, 2 sai trên 768 câu (ước tính +0,122 PA so với E0; số lạc quan vì regex viết theo train).
- Cả 2 câu sai đều là unanswerable.

**Thiết kế: frame parser đóng**

Cách hiện tại loại trừ bằng danh sách từ cấm, nên từ lạ vẫn lọt. Đề xuất đảo lại thành **thế giới đóng**: mọi từ có nghĩa trong câu phải được một slot giải thích, nếu không thì chuyển sang nhánh SQL.

```
"thang 3/2026 co bnhiu don bi huy vi giao tre"
 metric = cancel_count, period = month(2026,3), còn dư "vi giao tre"
 → không giải thích được → nhánh SQL
```

Cách này xử lý đúng cảnh báo của mentor về hai câu gần nghĩa nhưng khác intent, vì không dựa vào độ tương đồng.

**Cổng vào nhánh nhanh (phải đạt tất cả):**
1. Mọi từ được giải thích.
2. Kỳ được giải ra duy nhất và có dữ liệu.
3. Tên được nối về đúng một `entity_key`.
4. Năng lực `trusted`.
5. Mệnh đề liên quan được bật.

**Các đợt mở rộng:** đại lượng đơn lẻ → so sánh, % thay đổi → lọc theo nhóm, kênh, khách, sản phẩm → top-k, ngày hoặc tháng cao nhất → macro đợt 1–2.

**Thí nghiệm:**

| mã | nội dung | đạt khi |
|---|---|---|
| F1 | precision và coverage trên train, đánh giá leave-family-out | precision ≥ 98% trên họ bị giữ lại |
| F2 | coverage trên public, chỉ đếm, và độ khớp với silver | mục tiêu coverage ≥ 30%; chưa phải dự đoán |
| F3 | đột biến câu hỏi: thêm mệnh đề phụ vào câu đã khớp | ≥ 99% rơi về nhánh SQL |
| F4 | biến thể rulebook và shop `suspect` | nhánh nhanh tắt đúng |

**Lưu ý:** bộ từ của parser chỉ xây từ train. Public chỉ dùng để đo, tránh quá khớp.

### W6. Nhánh Text-to-SQL

| việc | mô tả | trạng thái hiện tại |
|---|---|---|
| Tạo DB mỗi shop | Xuất bảng sạch sang `cache/duckdb/<split>/`; `sales_all` chưa cắt `as_of`; bảng gốc thành `raw_*` | **xong** |
| Views | `sales_lines`, `orders`, `dim_*`, `calendar` xong; các view theo năng lực chờ W4 | **một phần** |
| Thẻ schema cho SQL | Views + bảng `raw_*` + mô tả + giá trị mẫu + macro của shop | **xong** (`sql/agent.py`) |
| Prompt SQL | Quy ước đầu ra, rulebook, schema, ví dụ, kỳ đã giải, tên đã nối | **xong** |
| Đầu ra | Một câu SELECT, kèm dòng `-- type: money` hoặc `UNANSWERABLE: lý do` | **xong** |
| Kiểm tra trước khi chạy | `extract_statements`: đúng một SELECT; binder bắt sai tên cột | **xong** (`run_select`) |
| Thực thi | Kết nối read-only, `set variable as_of`, chặn truy cập ngoài và đồng hồ thật, timeout, `threads=1` | **xong**, chạy bằng `--engine sql` |
| Sửa lỗi | Một vòng, kèm `Candidate bindings` | **xong** |
| Chuyển kết quả thành đáp án | Từ tập dòng sang kiểu đáp án; dùng lại `finalize`, `check_answer`, `invariants` | **xong** (`to_answer`) |
| Ví dụ SQL | Rejection sampling trên **train**: giữ SQL khớp đáp án | chưa có |
| Tái lập | `threads=1`, tiền là số nguyên, bắt buộc `ORDER BY` kèm mã | chưa có |

## 5. Thứ tự và cổng quyết định

**Hạn nộp: 2026-11-15**, còn 10 ngày tính từ 2026-11-05. Toàn bộ kế hoạch cần khoảng 13–14 ngày cho một người, nên phải cắt. Docker đang hoãn nhưng vẫn là điều kiện để nộp, nên giữ 3 ngày cuối cho phần này.

| ngày | nội dung | phụ thuộc |
|---|---|---|
| 06/11 | W6 trên `text2sql`, chạy Kaggle; rồi baseline trên `codegen` | W1 (xong) |
| 07/11 | Trạng thái lạ, bộ đột biến (dùng chung); ví dụ SQL; phân tích lỗi baseline | |
| **08/11** | **G1: so SQL với pandas** và W3 rule registry | |
| 09–10/11 | W4 đợt 1: khách, công nợ, trên nhánh thắng | W1 |
| 11/11 | W5 nhánh nhanh, các đợt đầu. Frame xuất theo dạng IR để dùng lại | W3, W4 |
| 12/11 | W2: kiểm định mapping, trạng thái `trusted`/`suspect`/`absent`, map cột bằng LLM | |
| 13–15/11 | Docker, chạy thử, nộp | |

**Tạm cắt:** W4 đợt 2–3, thí nghiệm F3–F4. Làm lại nếu còn thời gian, theo thứ tự này. Nếu G1 cho thấy SQL không hơn pandas, thời gian W6 còn lại chuyển sang W4.

**Hai nhánh song song (quyết định 2026-11-06).** `text2sql` và `codegen` cùng chạy trên Kaggle đến G1. Code dùng chung (ingest, canon, preprocess, postprocess, eval) đi qua `main` rồi merge xuống cả hai, nếu không G1 sẽ đo khác biệt dữ liệu chứ không phải phương pháp. Mỗi nhánh dùng dataset Kaggle riêng (`DM_DATASET`), và `BUILD.txt` trong kết quả ghi code đã chạy. Sau G1 chỉ một nhánh nhận tính năng mới, vì viết W4 cho cả `dm` lẫn macro là làm hai lần.

**Chưa làm theo phản hồi semantic (2026-11-06), chờ số liệu:** Semantic Specification đầy đủ (rulebook train và public: 64/67 mệnh đề chung giống hệt, thay đổi chủ yếu là thêm mệnh đề, nên W3 là đủ); Query IR dùng chung (làm trước G1 sẽ làm bẩn phép so sánh); thời gian bằng LLM, embedding cho tên, phân loại dòng bằng LLM, verifier. Mở lại nếu phân tích lỗi sau G1 chỉ ra đó là nguồn mất điểm chính. Mọi bước semantic khi nộp chạy bằng model local ≤ 15B, greedy, có cache, nên phải đo trên Qwen.

**G1.** Cùng model Qwen3.5-9B, đủ 3000 câu train (khoảng 3 giờ mỗi nhánh), cùng views và cùng định nghĩa. Ví dụ few-shot lấy từ shop khác ở cả hai nhánh. So sánh PA theo cặp với bootstrap CI, tỉ lệ lỗi thực thi, số token và thời gian mỗi câu.

| kết quả G1 | hành động |
|---|---|
| ΔPA ≥ 0 trong CI, hoặc lỗi thực thi giảm rõ mà PA không giảm | chọn SQL làm nhánh chính |
| SQL thua rõ ở câu nhiều bước nhưng thắng ở câu đơn giản | giữ SQL cho câu đơn giản, cân nhắc mở lại sinh code cho câu khó |
| SQL thua rõ tổng thể | quay lại sinh code, giữ W1–W5 |

## 6. Silver label bằng Gemini (tạm dừng)

Nhóm chưa cần kết quả public test, và có thể tự chạy model trên Vast AI nếu rẻ hơn. Backend `gemini` hiện chỉ cho chạy trên train. Phần dưới giữ lại để dùng khi mở lại.

**Model được chọn: `gemini-3-flash-preview`.** Không model nào trả lời đúng hết, nên đây là silver label chứ không phải đáp án thật.

**Thử nghiệm:** 60 câu train (`assets/dev60.txt`, có đáp án thật), pipeline pandas hiện tại, `--normalize rules`, greedy, 1 vòng sửa. Chưa gửi câu public nào. Chi phí tính theo bảng giá trả phí ngày 2026-11-05, chưa dùng Batch.

| model | đúng / sai / bỏ qua | PA | precision | chi phí 60 câu | ước tính 1000 câu |
|---|---|---|---|---|---|
| `gemini-2.5-flash-lite` | 38 / 8 / 14 | 0,50 | 82,6% | $0,05 | $0,9 |
| `gemini-3.1-flash-lite` | 44 / 7 / 9 | 0,62 | 86,3% | $0,14 | $2,4 |
| `gemini-3.5-flash-lite` | 50 / 8 / 2 | 0,70 | 86,2% | $0,24 | $4,0 |
| **`gemini-3-flash-preview`** | **54 / 5 / 1** | **0,82** | **91,5%** | $0,26 | **$4,3** |
| `gemini-3.8-flash` | 53 / 5 / 2 | 0,80 | 91,4% | $0,79 | $13,2 |

`gemini-3.1-pro-preview` và `gemini-4-argon` chưa thử được: chỉ có bản trả phí, và tài khoản đã hết credit trả trước.

**Cách đọc:**
- 60 câu chỉ cho độ chính xác xấp xỉ: 54/60 tương ứng khoảng 80–95% (khoảng tin cậy 95%). Kết quả không tách được `gemini-3-flash-preview` với `gemini-3.8-flash`, nên chọn model rẻ hơn.
- **Lỗi chủ yếu nằm ở pipeline, không ở model.** 4 câu mọi model đều sai; 3 câu là do biến thể tên (Coca Cola ở `biz_101`, top khách ở `biz_119`, hàng bán chạy ở `biz_125`). Tính lại bằng `product_key`, `customer_key` trong DuckDB ra đúng đáp án cả 3 câu. Sau khi đưa khoá tên vào pipeline, chạy lại đạt 58/60 (xem W1).
- **Cho hai model đồng thuận gần như không tăng độ tin cậy.** Khi `gemini-3-flash-preview` và `gemini-3.8-flash` cùng đáp án, 51/55 đúng (93%), gần bằng model đơn lẻ, vì hai model dùng chung pipeline nên sai giống nhau.

**Nếu mở lại:**
1. Chọn tier (xem dưới).
2. Đo độ tin cậy nhãn trên một tập train lớn hơn, không trùng 60 câu này, và ghi cùng mọi kết quả dùng silver.

**Tier của API key:** model có free tier gọi được, model chỉ trả phí báo hết credit, nên pilot nhiều khả năng chạy ở free tier. Theo trang giá, dữ liệu free tier được Google dùng để cải thiện sản phẩm. Với public test, nên nạp credit và xác nhận đang chạy tier trả phí trước khi gửi.

**Quy tắc sử dụng:**
- Chỉ dùng để đánh giá. Không đưa vào huấn luyện, few-shot hay bộ từ của parser.
- Lưu riêng ở `outputs/`, không đưa vào `assets/`.
- Chỉ áp dụng cho public test, **không bao giờ** gửi private test ra ngoài.
- Đây là silver, không phải đáp án thật.
