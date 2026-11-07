# DataMate: Proposal v4. Hai luồng xử lý và lỗi theo từng tầng

**Ngày:** 2026-11-08. Hạn nộp 2026-11-15.

## 0. Kết quả hiện tại

Số liệu ứng với code mới nhất của hai nhánh.

| luồng | commit | train (đúng / sai / bỏ qua) | dev120 | public (đúng / sai / bỏ qua) | precision public |
|---|---|---|---|---|---|
| **B. text-to-SQL** | `text2sql` `3903322` | **0.882** (2756 / 110 / 134) | 0.875 | **0.590** (729 / 139 / 132) | 84% |
| **A. pandas codegen** | `codegen` `07f9e96` | 0.875 (2723 / 97 / 180) | 0.867 | 0.565 (712 / 147 / 141) | 83% |
| **kết hợp A + B** (mục 3) | — | 0.912 | — | **0.640** | — |

**Cách đo:**
- Các câu trả lời model đã sinh ở lần chạy Kaggle gần nhất được cho chạy lại qua code mới nhất, không gọi lại model.
- Đối chứng: code cũ chạy cùng cách tái tạo đúng kết quả Kaggle, 4000/4000 câu với SQL và 3998/4000 câu với pandas.
- Nếu code mới cần một câu trả lời mới của model, câu đó được tính là bỏ qua.
- Điểm public chấm bằng bộ nhãn nội bộ, chỉ để phân tích. Bộ nhãn này khớp leaderboard với sai lệch dưới 0.01. Code mới nhất chưa được nộp lên leaderboard.

**Nhận xét:**
- **Hai luồng ngang nhau.** SQL − pandas: public +0.025, CI 95% [−0.015, 0.065]; train +0.007, CI [−0.011, 0.023].
- **Khoảng cách train → public khoảng 0.3 PA.**
  - Ngành đã gặp: public 0.696 (SQL) và 0.682 (pandas).
  - 4 ngành mới (dev_011–014): chỉ 0.190 và 0.124.

---

## 1. Luồng B: text-to-SQL

### 1.1 Tổng quan

```
[1] Ingest        file thô ─────────────► bảng sạch + vai trò cột
[2] Chuẩn hoá     bảng sạch ────────────► sales_lines, orders, fact (nợ, nhập, tồn, lô, giao)
[3] Semantic      sales_lines/orders/fact ► file DuckDB: view cắt theo as-of + macro định nghĩa
[4] Tiền xử lý    câu hỏi ──────────────► câu chuẩn hoá, kỳ thời gian, thực thể, loại đáp án
[5] Prompt        rulebook, README, schema card, ví dụ ► prompt 13–16k token
[6] Sinh SQL      prompt ───────────────► một câu SELECT, dòng đầu "-- type: <loại>"
[7] Lint + chạy   SELECT ───────────────► đáp án có kiểu, hoặc lỗi
[8] Kiểm tra      đáp án ───────────────► chấp nhận / sửa 1 lần / bỏ qua / thay bằng macro
[9] Quyết định    kết quả ──────────────► một dòng JSONL hoặc bỏ qua
```

- Model: Qwen3.5-9B (vLLM), temperature 0, 1 mẫu, tối đa 1 lần sửa.
- **Ví dụ xuyên suốt:** câu `q_64045044c253` của cửa hàng dev_003 (quán cà phê, dữ liệu từ phần mềm bán hàng lẫn sổ tay): *"Th 3/25 quan ban ra bao nhieu cai Banh croissant?"*. Hệ thống trả 112, đúng.

**Điểm mất theo tầng** (bỏ qua mất 1, sai mất 2):
- Tầng 6–8 được gán tự động từ log.
- Lỗi "sai giá trị, đúng loại" thường bắt nguồn từ tầng 1–5, nên được audit tay trên mẫu ngẫu nhiên (30 câu public, 15 câu train). Cả 45 câu vẫn sai với code mới nhất.

| | train | public |
|---|---|---|
| tầng 1–5 (chỉ thấy qua audit) | 4/15 câu trong mẫu | 20/30 câu trong mẫu |
| tầng 6 Sinh SQL (gán tự động) | 146 điểm | 153 điểm |
| tầng 7 Lint & thực thi | 15 | 4 |
| tầng 8 Kiểm tra & sửa | 27 | 17 |
| nhóm sai giá trị, đúng loại (chia theo mẫu audit) | 166 | 236 |
| **tổng** | **354** | **410** |

### Tầng 1. Ingest

**Làm gì:**
- Đọc mọi file XLSX / CSV / SQLite bằng calamine.
- Tìm dòng tiêu đề (bỏ dòng tiêu đề lớn, dòng lọc, dòng trống) và gộp header 2 dòng.
- Nối các sheet theo tháng và các file tách theo năm.
- Chuẩn hoá ngày (dd/mm/yyyy, số serial Excel, "1 thg 3 2025"), tiền ("84,000", "55k", "1,6tr") và số lượng có đơn vị ("3 chai").
- Gán vai trò cho cột: `order_id`, `order_date`, `status`, `product_name`, `line_amount`, `unit`, `customer_name`…

**Đầu vào:** `BanHang_TongHop_2025.xlsx` (5 sheet). Trong cùng cột `Ngày HĐ`, ngày được ghi theo ba kiểu:

```
Đơn số     | Tổng thanh toán | Ngày HĐ      | Trạng thái giao | Dòng | Hàng hóa       | Thành tiền (đ)
ORD000001  | 84,000          | 1 thg 3 2025 | Xong            | 1    | Cà phê sữa (M) | ...
ORD000002  | 90,000          | 01/03/2025   | Xong            | 1    | ...
ORD000003  | 100,000         | 45717.0      | Xong            | 1    | ...
```

**Đầu ra:** bảng `BanHang_TongHop.BanHang` gồm 100 210 dòng (năm 2025 và 2026 nối lại), kiểu dữ liệu đúng, mỗi cột có vai trò:
- `Đơn số` → order_id;
- `Ngày HĐ` → order_date;
- `Trạng thái giao` → status {Xong, Đã hủy đơn, Khách trả, Đang vận chuyển};
- `Hàng hóa` → product_name;
- `Thành tiền (đ)` → line_amount.

**Lỗi:** không có lỗi nào trong mẫu audit có nguyên nhân chính ở tầng này.

### Tầng 2. Chuẩn hoá & facts

**Làm gì:**
- Dựng `sales_lines` (1 dòng = 1 dòng hàng):
  - lấp ô trống ở dòng tiếp theo của cùng đơn;
  - nối bảng đơn với bảng chi tiết;
  - tính `counted` (đơn được tính doanh thu theo rulebook: không huỷ, không có dòng trả);
  - quy số lượng về đơn vị gốc (`qty_base`);
  - gộp các cách viết khác nhau của tên hàng và tên khách;
  - đánh dấu khách lẻ (`walkin`) và hàng hoá thật (`is_goods`).
- Dựng `orders` (1 dòng = 1 đơn).
- Dựng các fact theo grain của rulebook: nợ theo đơn, thanh toán, phiếu nhập, thẻ kho, lô, chuyến giao.

**Đầu vào:** các bảng của tầng 1 cùng vai trò cột.

**Đầu ra:** `sales_lines` của dev_003 có 100 210 dòng, với các cột chuẩn `order_id, date, status, counted, amount, qty, qty_base, unit, product_name, customer, walkin` cùng các cột gốc. Với ví dụ xuyên suốt, các dòng `product_name = 'Bánh croissant'` có `counted = true` trong tháng 3/2025 cộng lại được `qty_base` = 112.

**Lỗi:** public 2/30 trong mẫu, cộng thêm các lỗi tìm thấy ngoài mẫu.

**(a) Đồ uống có topping ghi chung ô bị tách thành mặt hàng riêng.** Public rulebook có quy tắc cho topping ghi chung ô.
- Bảng: `sales_lines` (dev_002), cột `product_name`.
- Đầu vào: "Tháng 4/2026 bán ra bao nhiêu ly Latte (M)?"

  ```
  product_name                         | qty_base (4/2026, counted)
  Latte (M)                            | 118
  Latte (M) + Pudding trứng (10.000đ)  | 1
  Latte (M) + Trân châu đen (7.000đ)   | 2
  ```
- Đầu ra mong muốn: **121** (118 + 1 + 2), khớp gold.
- Đầu ra thực tế: **118**. Model lọc `product_name = 'Latte (M)'`, nên mất 3 ly có topping.

**(b) Phí dịch vụ được đánh dấu là hàng hoá.**
- Bảng: `sales_lines` (dev_008), có dòng `product_name = 'Phí phục vụ'` với `is_goods = true` (6 770 dòng).
- Đầu vào: câu hỏi về món bán chạy nhất.
- Đầu ra mong muốn: "Bia Sài Gòn Special lon".
- Đầu ra thực tế: "Phí phục vụ".

**(c) Tên khách có số điện thoại: không phải lỗi, mà do bộ nhãn nội bộ.**
- Bảng: `khach_hang` (dev_010), cột `Khách lẻ/sỉ` chứa "Dương Giang Hà - 0701 466 949". `strip_phone` cắt số điện thoại, nên `sales_lines.customer_name` = "Dương Giang Hà".
- Bộ nhãn public nội bộ ghi tên kèm số điện thoại và không có alias, nên 2 bảng "top khách nợ" của dev_010 bị chấm sai dù số tiền đúng.
- Trên train, có 4 cửa hàng mà danh sách khách ghi tên kèm số điện thoại (biz_106, 108, 111, 120). Ở đó gold ghi tên **không** có số điện thoại, còn alias chứa cả tên có số điện thoại và mã khách. Ví dụ biz_106: gold "Vương Phương Loan", alias `["KH0185", "Vương Phương Loan - 0973 189 069"]`.
- Bộ chấm chính thức nhận mọi cách viết trong alias, nên cách làm hiện tại đúng. Điểm public nội bộ chỉ thấp hơn thực tế một chút ở các câu này.

**(d) Nợ chỉ được ghi trong ghi chú tự do, nên không có fact nợ.**
- Bảng: `ChiTietHoaDon` (dev_008), cột `Note`: "nợ 79k, khách ck đủ rồi", "nợ 611 nghìn, khách ck đủ rồi" (671 dòng).
- Store có `debt_orders_all`, nhưng không có bảng thanh toán, nên không tạo được macro nợ nào.
- Đầu vào: "Đến hết ngày 22/07/2026, số khách còn công nợ là bao nhiêu?"
- Đầu ra mong muốn: **15**.
- Đầu ra thực tế: **131**. Model đếm mọi khách có chữ "nợ" trong `Note`, kể cả khi ghi chú nói đã trả đủ.

### Tầng 3. Semantic layer: view và macro DuckDB

**Làm gì:** mỗi cửa hàng có một file DuckDB chỉ đọc, gồm:
- view `sales_lines` / `orders` tự cắt ở `as_of` (bản ghi sau ngày hỏi "chưa xảy ra");
- bảng gốc `raw_*`;
- macro cho từng định nghĩa của rulebook, chỉ tạo khi cửa hàng có đủ dữ liệu: `revenue(a,b)`, `order_count`, `cancel_rate`, `avg_order_value`, `debt_by_order(x)`, `debt_by_customer(x)`, `debt_repaid`, `stock_at(p,d)`, `lots_left(d)`, `price_effect(p,d,n)`, `slow_products`, `avg_import_price`, `shift_of(ts)`, `has_name`, `same_text`.

**Đầu vào:** `sales_lines`, `orders` và fact của tầng 2.

**Đầu ra:** ví dụ:
- `revenue(date '2025-03-01', date '2025-03-31')` trả doanh thu tháng, đã loại đơn huỷ hoặc trả hàng, và trả NULL nếu kỳ nằm ngoài dữ liệu.
- `debt_by_customer(date '2026-07-31')` trả bảng `customer, customer_name, debt` đã sắp xếp.

**Lỗi:** train 4/15 trong mẫu; public 16/30, chủ yếu ở ngành mới.

**(a) View `orders` chỉ giữ thuộc tính dòng hàng của dòng đầu tiên trong đơn** (public 4/30).
- Bảng: `sales_lines` và `orders` (dev_001). Trong `sales_lines`, đơn `ORD014837` có 4 dòng thuộc các nhóm nước giải khát, gạo, nước giải khát, thuốc lá. Trong `orders`, đơn này có `Nhóm hàng hóa = 'nước giải khát'`, tức chỉ nhóm của dòng đầu.
- Đầu vào: "Tháng 9/2025 số đơn có mua hàng nhóm bánh kẹo là bao nhiêu?"
- Đầu ra mong muốn: **229**, tính bằng `count(distinct order_id)` trên `sales_lines` có nhóm bánh kẹo. Khớp gold.
- Đầu ra thực tế: **82**. `select count(*) from orders where has_name("Nhóm hàng hóa", 'bánh kẹo')` chỉ đếm đơn có dòng đầu là bánh kẹo.

**(b) Tiền cấp đơn không được đánh dấu grain** (public 2/30).
- Trong schema card của dev_003, view `sales_lines` liệt kê `Tổng thanh toán`, `Phí ship`, `Công nợ`, `Tiền thu` như mọi cột khác, không ghi đây là cột cấp đơn.
- Bảng: `raw_sobanhang_so_ban_hang` (dev_011). `Khách trả` lặp lại trên mọi dòng của đơn:

  ```
  Ngày       | STT | Hàng      | Thành tiền | Bớt   | Khách trả
  2025-06-01 | 1   | Sạc 25W SS| 580000     | 10000 | 720000
  2025-06-01 | 1   | Cáp C-L   | 150000     | 10000 | 720000
  ```
- Đầu vào: "6-2025 tổng tiền khách phải trả là bao nhiêu?"
- Đầu ra mong muốn: **871 448 000**, lấy mỗi đơn một lần. Khớp gold.
- Đầu ra thực tế: **1 852 184 000**, do cộng `Khách trả` theo từng dòng.

**(c) Không có cách tìm ngày đổi giá** (train 2/15, public 2/30).
- Bảng: `raw_banhang_tonghop_2025_banhang_lichsugia` (dev_003) có dòng `Trà vải (L) | Ngày áp dụng 2026-04-06 | Giá cũ 45000 | Giá mới 47000`.
- Đầu vào: "Từ hồi Trà vải (L) lên giá 47.000đ, so 30 ngày kể từ ngày đổi giá với 30 ngày trước đó, số ly bán ra tăng hay giảm bao nhiêu %?" Câu hỏi không ghi ngày đổi giá.
- Đầu ra mong muốn: `price_effect('Trà vải (L)', date '2026-04-06', 30)` = **−3.6**. Khớp gold.
- Đầu ra thực tế: model truyền ngày hôm nay, `price_effect('Trà vải (L)', date '2026-08-21', 30)` = **−95.05**.

**(d) Không có định nghĩa cho quy tắc của ngành mới** (public 7/30): nợ trả góp, phiếu bảo hành, hoa hồng thợ, doanh thu tiệm sửa xe.
- Bảng: `raw_sono_tra_gop` (dev_014). Mỗi hợp đồng có một dòng đầu, kèm một dòng cho mỗi kỳ đã góp; các ô hợp đồng để trống ở dòng góp:

  ```
  Ngày       | STT | Khách        | Số kỳ | Góp/tháng | Hạn góp (ngày) | Ngày góp   | Đã góp
  2025-07-03 | 33  | Đinh Mai Hoa | 6     | 2600000   | 3              | 2025-08-05 | 2600000
             | 33  |              |       |           |                | 2025-09-03 | 2600000
  ```
- Rulebook (mục điện thoại): "Kỳ góp có hạn **trước** ngày hỏi mà chưa góp đủ là **quá hạn**."
- Đầu vào: "Đến ngày 13/03/2026 có mấy khách trả góp còn nợ kỳ góp quá hạn?"
- Đầu ra mong muốn: **16**. Tính lại bằng cách lấy số kỳ đã đến hạn trước ngày hỏi nhân `Góp/tháng`, so với tổng `Đã góp`. Khớp gold.
- Đầu ra thực tế: **122**. Model đếm dòng có `"Đã góp" < "Trả trước"`.

**(e) `has_name` khớp chuỗi con** (train 2/15, public 1/30).
- Bảng: `sales_lines` (biz_111), cột `Nhom hang` có hai giá trị 'trà' và 'trà sữa'.
- Đầu vào: "Doanh thu nhóm hàng trà chiếm bao nhiêu phần trăm tổng doanh thu tháng 2/2025?"
- Đầu ra mong muốn: lọc `same_text("Nhom hang", 'trà')` cho **18.92**. Khớp gold.
- Đầu ra thực tế: `has_name("Nhom hang", 'trà')` bắt cả 'trà sữa', ra **40.04**.
- Chiều ngược lại cũng sai (dev_012). Cột `Làm gì` ghi "vá", "vá xe", "vá ruột", "nhớt Honda + vá xe"… nên `has_name("Làm gì", 'vá ruột')` chỉ được **29** phiếu. Gold là 98; đếm theo chữ "vá" và bỏ phiếu bảo hành như rulebook ra khoảng 99.

### Tầng 4. Tiền xử lý câu hỏi

**Làm gì:**
- Chuẩn hoá bằng luật: Telex, bỏ dấu, viết tắt (`dthu`, `bnhiu`, `th7/2026`), phương ngữ (`ni`, `rứa`, `mô`).
- Đổi cách viết kỳ thời gian thành khoảng ngày theo lịch của đề.
- Tìm tên thực thể (hàng, khách, nhóm, nhân viên) trong danh mục của cửa hàng.
- Một lượt LLM viết lại câu và đoán loại đáp án.

**Đầu vào:** câu hỏi gốc và ngày as-of.

**Đầu ra:** ví dụ xuyên suốt:

```
"Th 3/25 quan ban ra bao nhieu cai Banh croissant?"
→ câu:   "Tháng 3/2025 quán bán ra bao nhiêu cái Bánh croissant?"
→ kỳ:    "thang 3/2025" -> month(2025, 3) = 2025-03-01..2025-03-31
→ tên:   product "Bánh croissant" (exact)
→ loại:  number
```

**Lỗi:** public 1/30 trong mẫu, cộng một lỗi lộ ra ở tầng 8.

**(a) Tên chung bị gán cho một biến thể.**
- Bảng: `sales_lines` (dev_014). Trong tháng 9/2026 bán `iPhone 13 128GB Trắng` 3 chiếc, `iPhone 13 128GB cũ 98%` 6 chiếc, `iPhone 13 Pro Max 128GB cũ 99%` 6 chiếc.
- Đầu vào: "iPhone 13 th này bán đc mấy chiếc rồi?"
- Tầng này gán tên: `product "iPhone 13 128GB Trắng" (partial match)`.
- Đầu ra mong muốn: **9**, gồm mọi biến thể "iPhone 13" trừ dòng Pro Max. Khớp gold.
- Đầu ra thực tế: **3**.

**(b) Đoán sai loại đáp án.**
- Đầu vào: "tháng này đơn trả tiền mặt với đơn trả ví điện tử, từng bên doanh thu bao nhiêu" (dev_010).
- Tầng này đoán loại đáp án là `string`.
- Đầu ra mong muốn: list **[62 390 000, 7 123 000]**.
- Đầu ra thực tế: SQL lần đầu đã ra đúng list này, nhưng tầng 8 chặn vì "câu hỏi cần string". Lần sửa bị lint chặn, nên câu bị bỏ qua.

### Tầng 5. Dựng prompt

**Làm gì:** ghép các phần sau:
- rulebook (toàn văn, khoảng 24k ký tự);
- README của cửa hàng;
- schema card SQL: mọi view, bảng gốc và macro mà cửa hàng có, mỗi cột có kiểu, giá trị mẫu và vai trò. Card của dev_003 dài khoảng 15k ký tự;
- 4 ví dụ train lấy theo độ giống câu hỏi, luôn từ cửa hàng khác;
- các gợi ý của tầng 4.

**Đầu vào / đầu ra:** các phần trên → một prompt 13–16k token. Ví dụ đoạn schema card:

```
## View sales_lines  rows=100210
- "counted" | float | False .. True
- "qty_base" | int | 1 .. 6
- "product_name" | str | Cà phê sữa (M), Trà đào cam sả (M) … (50 distinct)
- "Tổng thanh toán" | int | 7000 .. 443000
```

**Lỗi:** public 1/30 trong mẫu, cộng một lỗi lộ ra ở tầng 7 (xem tầng 7 (a)).

**(a) Rulebook đưa vào toàn văn, không chỉ ra điều khoản liên quan.**
- Rulebook dòng 55: "Nhân viên, thợ chăm chỉ nhất: người có **nhiều ngày làm việc nhất** trong kỳ; ngày làm việc là ngày có ít nhất một đơn, kể cả phiếu bảo hành".
- Bảng: `orders` (dev_012), tháng 9/2025:

  ```
  Thợ           | số ngày làm | số phiếu (counted)
  Hà Loan Đạt   | 16          | 97
  Lương Chi Đức | 15          | 113
  ```
- Đầu vào: "Trong tháng 9/2025, ai là thợ chăm chỉ nhất?"
- Đầu ra mong muốn: **Hà Loan Đạt**, nhiều ngày làm nhất. Khớp gold.
- Đầu ra thực tế: **Lương Chi Đức**. Model tự đếm số phiếu (`count(distinct order_id)`).

### Tầng 6. Sinh SQL (Qwen3.5-9B)

**Làm gì:** model đọc prompt và trả về một khối ```sql, dòng đầu khai báo loại đáp án (`money`, `count`, `percent`, `list`, `table`…), hoặc một dòng `UNANSWERABLE: lý do`. Khi sửa, model nhận thêm thông báo lỗi của lần trước.

**Đầu vào:** prompt.

**Đầu ra:** ví dụ xuyên suốt:

```sql
-- type: number
select sum(qty_base) from sales_lines
where counted and product_name = 'Bánh croissant' and "date" between date '2025-03-01' and date '2025-03-31'
```

**Lỗi gán tự động** (điểm mất, train / public):

| lỗi | train | public |
|---|---|---|
| không nhận ra câu không trả lời được | 55 | 25 |
| kết quả rỗng (lọc sai thực thể hoặc kỳ) | 18 | 38 |
| dùng cột không tồn tại | 12 | 32 |
| lỗi logic SQL (GROUP BY, subquery…) | 16 | 21 |
| sai số phần tử k / sai loại đáp án | 16 / 14 | 6 / 6 |
| thứ tự, làm tròn | 2 | 12 |
| khác (cú pháp, bảng hoặc hàm không tồn tại) | 13 | 13 |

**Audit nhóm sai giá trị:** train 11/15, phần lớn là đọc sai câu hỏi; public 10/30, phần lớn là grain và cách viết truy vấn.

**(a) Bỏ mất điều kiện ca** (train 3/15).
- Bảng: `sales_lines` (biz_128), tuần 08–14/06/2026.
- Đầu vào: "Ca chiều tuần trước doanh thu được bao nhiêu?"
- Đầu ra mong muốn: `sum(amount) filter (shift_of(order_ts) = 'chiều')` = **112 133 300**. Khớp gold.
- Đầu ra thực tế: `select revenue(date '2026-06-08', date '2026-06-14')` = **195 905 200**, tức doanh thu cả tuần.

**(b) Chỉ lọc một món khi câu hỏi hỏi tất cả** (train 2/15).
- Đầu vào: "cộng mọi món tính theo ly, tuần trước bán ra được mấy ly" (biz_106).
- Đầu ra mong muốn: **1594**.
- Đầu ra thực tế: **105**, do lọc `product_name = 'Cà phê sữa (M)'`.

**(c) Nhầm tiền với số lượng** (train 2/15).
- Đầu vào: "tháng 9/2025 Chân gà ngâm sả tắc bán được bao nhiêu đĩa" (biz_129).
- Đầu ra mong muốn: **182** đĩa.
- Đầu ra thực tế: **15 470 000**, do `sum(amount)` và khai báo `-- type: money`.

**(d) Đếm dòng thay vì đếm đơn** (public).
- Bảng: `sales_lines` (dev_005), tháng 5/2025.
- Đầu vào: "Bao nhiêu % khách mua từ 6 đơn trở lên, tính trên số khách có mua?"
- Đầu ra mong muốn: đếm `count(distinct order_id)` cho mỗi khách, ra **33.84**. Khớp gold.
- Đầu ra thực tế: `count(*) as cnt from sales_lines group by customer_key` đếm dòng hàng, ra **73.17**. Lint `count(*)` không bắt được vì có `as cnt`.

**(e) Sai thứ tự ưu tiên AND/OR** (public).
- Bảng: `sales_lines` (dev_014), cột `Loại` {máy mới, máy cũ, phụ kiện}.
- Đầu vào: "Th8/2025 bán được bao nhiêu máy không tính phụ kiện?"
- Đầu ra mong muốn: `... and (has_name("Loại",'máy mới') or has_name("Loại",'máy cũ'))` = **167**. Khớp gold.
- Đầu ra thực tế: thiếu ngoặc, nên vế `or` lấy máy cũ của mọi tháng và mọi trạng thái, ra **731**.

**(f) Đọc bảng gốc thay vì view đã chuẩn hoá** (public).
- Bảng: `raw_sosuaxe` (dev_012). Cột `Thợ` ghi biệt danh "A.An", "An", "a An". Trong view `sales_lines`, các biệt danh này đã được gộp thành "Đàm Lan An".
- Đầu vào: "Thợ Đàm Lan An tháng này làm được tổng bao nhiêu tiền rồi?"
- Đầu ra mong muốn: lọc `"Thợ" = 'Đàm Lan An'` trên `sales_lines` cho **25 180 500**. Khớp gold.
- Đầu ra thực tế: `has_name("Thợ", 'Đàm Lan An')` trên `raw_sosuaxe` cho NULL, nên câu bị bỏ qua.

**(g) Không nhận ra câu không trả lời được** (train 55 điểm, public 25 điểm).
- Bảng: `sales_lines` (dev_002) không có cột nhóm hàng nào.
- Đầu vào: "Tháng 11/2025, nhóm trà có mặt hàng nào bán được nhiều ly nhất?"
- Đầu ra mong muốn: `UNANSWERABLE`, đúng như gold.
- Đầu ra thực tế: model thay bằng `has_name(product_name, 'trà')` và trả "Trà đào cam sả (M)".

### Tầng 7. Lint & thực thi

**Làm gì:**
- **Lint** chặn trước khi chạy các mẫu truy vấn luôn sai, rồi trả thông báo cho model sửa:
  - nhân `cancel_rate()` với 100;
  - `count(*)` trên `sales_lines`;
  - `avg()` trên giá nhập;
  - `has_name` trên hoạt chất;
  - đếm `orders` mà thiếu `counted`;
  - `LIMIT` không có `ORDER BY`.
- **Tự viết lại** một số mẫu:
  - thêm `not walkin` khi group theo khách;
  - đổi tên khoá tự đặt (`customer_key`) về cột có thật.
- **Chạy** trên DuckDB chỉ đọc, timeout 30 giây.
- **Đổi kết quả thành đáp án có kiểu:** `money` → làm tròn đồng; `percent` → 2 chữ số; `list` → một cột…

**Đầu vào:** câu SELECT.

**Đầu ra:** `{"type": "number", "answer": 112}`, hoặc thông báo lỗi, ví dụ `Binder Error: Referenced column "Nhóm hàng" not found`.

**Lỗi:** train 15 điểm, public 4 điểm.

**(a) Lint chặn nhầm, vì prompt hướng dẫn sai tên cột** (train 6 câu, cùng ở biz_128).
- Bảng: view `orders` (biz_128). Trạng thái đã chuẩn hoá nằm ở cột `status`; view này không có cột `status__norm`.
- Prompt (tầng 5) lại viết: "Status columns with role=status have a normalized copy `<col>__norm`".
- Đầu vào: "Trong tháng 6/2026, cửa hàng có mấy đơn ở trạng thái huỷ?"
- SQL: `select count(*) from orders where "date" between ... and status__norm = 'huỷ'`.
- Lint "đếm `orders` phải có `counted` hoặc `status`" dùng regex `\bstatus\b`, không nhận ra `status__norm`. Nó báo "orders holds every status…". Model gửi lại đúng câu cũ, nên câu bị bỏ qua.
- Đầu ra mong muốn: **35**, lấy bằng `... and status = 'huỷ'`. Khớp gold. Năm câu còn lại của nhóm này cũng khớp gold khi sửa như vậy.
- Đầu ra thực tế: bỏ qua. Kể cả khi lint không chặn, câu này vẫn hỏng vì lỗi Binder (`status__norm` không tồn tại).

**(b) Lint bỏ sót:** xem tầng 6 (d).

**(c) Timeout:** train 4, public 1.

### Tầng 8. Kiểm tra kết quả & sửa

**Làm gì:**
- Kiểm tra đáp án:
  - đúng loại đáp án tầng 4 đoán;
  - đúng kỳ thời gian;
  - các bất biến (tiền không quá nhỏ, % hợp lệ, không âm…);
  - với câu khớp một định nghĩa đã kiểm thử, so với macro của định nghĩa đó.
- Có cảnh báo thì cho model sửa một lần. Cảnh báo cứng mà vẫn còn thì bỏ qua câu. Nếu chỉ lệch với macro thì lấy đáp án của macro.

**Đầu vào:** đáp án của tầng 7, loại đáp án và kỳ thời gian dự kiến.

**Đầu ra:** chấp nhận, sửa, bỏ qua, hoặc thay bằng đáp án của macro.

**Lỗi:** train 27 điểm, public 17 điểm.
- **Hiếm khi chặn nhầm:** chỉ 7 câu train và 3 câu public từng có một lần thử đúng.
- **Không bắt được câu sai:** 110 câu train và 139 câu public sai vẫn qua hết kiểm tra, vì kiểm tra chỉ thấy hình dạng của đáp án.

**(a) Bất biến chặn nhầm.**
- Đầu vào: "Đếm giùm t5/2025 có mấy đơn tiền hàng từ 760.000đ trở lên" (dev_013).
- SQL: `count(*) from orders where counted and goods_amount >= 760000 ...` = 211.
- Đầu ra mong muốn: **211**, đúng như gold.
- Đầu ra thực tế: bỏ qua. Kiểm tra coi 211 là tiền vì câu hỏi có "760.000đ" và báo "211 đồng is implausibly small for a VND amount". Model trả lại đúng câu đó, và câu vẫn bị chặn.

**(b) So với macro rồi thay đáp án biến câu đúng thành câu sai.**
- Đầu vào: "Th1/2025 bao nhiêu đơn có doanh thu từ 410.000 đồng trở lên?" (dev_008).
- SQL đếm đơn, ra 846. Đây là đáp án đúng.
- Kiểm tra khớp câu hỏi với định nghĩa `revenue` (vì có chữ "doanh thu") và báo lệch. Sau lần sửa vẫn lệch, nên đáp án bị thay bằng `revenue(...)`.
- Đầu ra mong muốn: **846**.
- Đầu ra thực tế: **858 023 000**, tức doanh thu tháng.

### Tầng 9. Quyết định & xuất

**Làm gì:** với 1 mẫu, nhận kết quả của tầng 8 và viết một dòng JSONL đúng định dạng của đề (tiền là số nguyên, % hai chữ số, ngày `YYYY-MM-DD`, list hoặc bảng), hoặc bỏ qua.

**Đầu vào / đầu ra:** `{"type": "number", "answer": 112}` → `{"id": "q_64045044c253", "type": "number", "answer": 112}`.

**Lỗi:**
- **Không phân biệt độ tin cậy theo loại cửa hàng.** Precision ở ngành đã gặp là 89.5%, ở ngành mới 61.6%; cùng một ngưỡng nên câu ở ngành mới vẫn được trả lời.
- **Câu không trả lời được:** đúng 24/70 trên train, 13/30 trên public; trả lời bừa 9 câu train và 8 câu public.

---

## 2. Luồng A: pandas codegen

Tầng 1, 2 và 4 dùng chung code với luồng B: cùng đầu vào, đầu ra và lỗi (mục 1, tầng 1, 2, 4). Trong mẫu audit 20 câu public của luồng A, có 2 câu do tầng 2 ("Phí phục vụ" bị tính là hàng). Phần dưới chỉ ghi chỗ khác.

| điểm mất | train | public |
|---|---|---|
| tầng 6 Sinh chương trình (gán tự động) | 222 | 187 |
| tầng 7 Lint & thực thi | 5 | 2 |
| tầng 8 Kiểm tra & sửa | 13 | 6 |
| nhóm sai giá trị, đúng loại | 134 | 240 |
| **tổng** | **374** | **435** |

### Tầng 3. Semantic layer: helper `dm.*`

**Đầu vào:** DataFrame `sales_lines()`, `orders()` và dict `T` chứa bảng gốc.

**Đầu ra:** các hàm Python đã kiểm thử trên đáp án train:
- kỳ thời gian: `month()`, `in_period()`, `this_month()`;
- định nghĩa: `revenue(p, lines)`, `avg_order_value`, `price_effect`, các hàm nợ, tồn, lô;
- xếp hạng và tra tên: `rank_top`, `find`;
- trả đáp án: `answer_number`, `answer_list`…

**Lỗi:** public 10/20 trong mẫu.
- **Cùng các lỗ hổng như luồng B:**
  - thiếu định nghĩa cho ngành mới (4): trả góp quá hạn trả 206, gold 16; xe sửa từ 2 lần trả 23, gold 33; doanh thu tiệm sửa xe; tỉ lệ bảo hành;
  - tiền cấp đơn không được đánh dấu grain (3): `Khách trả.sum() / len(lines)` cho bình quân mỗi đơn trả 5 420 835, gold 4 325 306;
  - `goods_amount` khác "doanh thu đơn" (1).
- **Riêng luồng A: `find()[0]` chỉ lấy biến thể đầu tiên** (2). "1-2026 bán được bao nhiêu chiếc Xiaomi Redmi Note 14" trả 5, gold 11; "iPhone 16 Pro Max" trả 4, gold 7.

### Tầng 5. Dựng prompt

Giống luồng B, nhưng thay schema card SQL bằng tài liệu helper và schema card của DataFrame. Ví dụ mẫu là chương trình Python.

### Tầng 6. Sinh chương trình Python

**Đầu ra:** ví dụ xuyên suốt:

```python
s = sales_lines()
p = month(2025, 3)
mine = s[s['product_name'] == 'Bánh croissant']
qty = mine.loc[mine['counted'] & in_period(mine['date'], p), 'qty_base'].sum()
answer_number(qty, kind='count')
```

**Lỗi gán tự động** (train / public):
- tên cột không tồn tại (KeyError): 81 / 66;
- không nhận ra câu không trả lời được: 38 / 36;
- kết quả rỗng: 26 / 32;
- nói không trả lời được khi có dữ liệu: 22 / 12;
- gọi sai API: 19 / 13.

**Audit public (8/20):**
- Thiếu điều kiện `counted` (2): đơn từ 180.000đ trả 448, gold 428.
- Đếm dòng làm số đơn (1): bình quân một đơn trả 63 811, gold 120 115.
- Lọc size bằng `str.contains('L')` (1): trả 2470, gold 1801.
- Mẫu số % sai (1): tỉ trọng Huggies trả 7.96, gold 29.25.
- Group theo tên khách thay vì mã khách, đơn bán chịu không lọc theo ngày hỏi, công thức tỉ lệ bảo hành (3).

### Tầng 7–9

- **Tầng 7:** lint chỉ ở dạng lời khuyên (chương trình không dùng helper đã kiểm thử, hoặc đếm dòng thay vì đơn, thì được yêu cầu viết lại một lần). Chạy trong sandbox, timeout 30 giây. Ít lỗi: 5 / 2 điểm.
- **Tầng 8:** giống luồng B. Chặn 13 / 6 điểm.
- **Tầng 9:** trả lời bừa nhiều hơn luồng B: 12 câu public không trả lời được lại có đáp án (SQL 8). Precision ở ngành mới chỉ 57.6%.

---

## 3. So sánh và kết hợp hai luồng

| public (code mới nhất) | số câu | SQL đúng | pandas đúng |
|---|---|---|---|
| cả hai trả lời và khớp | 679 | 635 (93.5%) | 635 |
| cả hai trả lời, khác nhau | 105 | 32 | 35 |
| chỉ SQL trả lời | 84 | 62 | — |
| chỉ pandas trả lời | 75 | — | 42 |

- **Train:** khi khớp, đúng 99.3% (2579/2597).
- **Trả lời khi khớp hoặc khi chỉ một luồng trả lời; bỏ qua khi khác nhau:** train 0.912, public **0.640**, tức +0.050 so với SQL, CI [0.022, 0.078].
- **Train so với public:**
  - Trên train, các tầng 1–5 gần như đủ. Lỗi dồn về tầng 6, chủ yếu là đọc sai câu hỏi.
  - Trên public, 20/30 câu trong mẫu audit bắt nguồn từ tầng 2–5: grain, topping, nợ ghi tự do, ngày đổi giá, quy tắc ngành mới.
  - Private có thêm ngành chưa gặp, nên rủi ro lớn nhất nằm ở tầng 2–3.

## 4. Đề xuất, xếp theo tầng

1. **Tầng 9, kết hợp hai luồng:** +0.050 trên public. Thời gian khoảng gấp đôi (2× T4: 4.3 + 5.2 s/câu); cần đo trên 2× 5090.
2. **Tầng 2–3, grain của cột:**
   - Đánh dấu mỗi cột là cấp đơn hay cấp dòng.
   - View `orders` chỉ mang cột cấp đơn; tiền cấp đơn chỉ lấy từ `orders`.
   - Lint khi cộng một cột cấp đơn trên dòng hàng.
3. **Tầng 2, chuẩn hoá còn thiếu:**
   - tách topping ghi chung ô thành một dòng riêng của mặt hàng topping;
   - phí phục vụ vẫn tính vào doanh thu, nhưng không xếp hạng và không đếm như một mặt hàng.
4. **Tầng 3, định nghĩa còn thiếu:**
   - macro tìm ngày đổi giá từ lịch sử giá;
   - `has_name` theo từ và theo biến thể đã gộp;
   - quy tắc ngành đọc từ rulebook: trả góp, bảo hành, hoa hồng.
5. **Tầng 5, rulebook theo câu hỏi:** đưa đúng điều khoản liên quan (định nghĩa "nhất", mục ngành) vào ngay cạnh câu hỏi, thay vì để model tự tìm trong 24k ký tự.
6. **Tầng 4, tên chung:** khi câu hỏi nêu tên chung (dòng máy, hãng, nhóm), gán cho mọi biến thể khớp chứ không chọn một.
7. **Tầng 6–7, kế hoạch trung gian:**
   - Model khai báo intent, metric, grain, bộ lọc, đơn vị và kỳ trước khi viết SQL.
   - Luật đối chiếu với câu hỏi, ví dụ có "ca chiều" mà thiếu lọc ca, hoặc hỏi "cộng mọi món" mà lại lọc một món.
   - Ưu tiên view đã chuẩn hoá hơn bảng `raw_*`.
8. **Tầng 5 và 7, tên cột trạng thái:**
   - Sửa câu trong prompt về `<col>__norm` (với view, tên cột là `status`).
   - Lint nhận cả `status__norm`.
   - Đổi `status__norm` thành `status` theo cùng cách đã làm cho `customer_key`.
9. **Tầng 8:**
   - Bất biến "tiền quá nhỏ" không áp dụng cho câu đếm.
   - Chỉ thay đáp án bằng macro khi metric và grain của câu hỏi khớp định nghĩa, không chỉ vì có từ khoá "doanh thu".
