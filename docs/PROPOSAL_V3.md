# DataMate: Đề xuất giải pháp và kế hoạch thí nghiệm

**Bối cảnh:** Zalo AI Challenge 2026, đề bài DataMate.
**Ngày:** 2026-11-04 (bản nháp để trao đổi với mentor).
**Câu hỏi nghiên cứu:** *Một semantic layer tất định (các định nghĩa nghiệp vụ được cài sẵn và kiểm thử) cải thiện được bao nhiêu về độ chính xác, độ tin cậy khi thực thi và khả năng tái lập của chương trình do LLM sinh ra, trong bài toán hỏi đáp trên dữ liệu có cấu trúc? Fine-tune bộ sinh mã có cải thiện thêm hay không?*

---

## Tóm tắt

Hệ thống nhận một câu hỏi tiếng Việt đời thường về dữ liệu bán hàng của một cửa hàng nhỏ và phải trả về một đáp án có kiểu, hoặc từ chối trả lời. Dữ liệu là các file CSV, Excel và SQLite xuất ra từ phần mềm bán hàng hoặc ghi tay.

Về hình thức, đây là bài toán gần với Text-to-SQL, nhưng khác ở ba điểm quyết định:
1. **Không có cơ sở dữ liệu sạch.** Mỗi cửa hàng một định dạng, tên cột và layout đều lộn xộn.
2. **Đáp án đúng là con số do ban tổ chức tính theo một "rulebook"** (định nghĩa doanh thu, đơn nào được tính, làm tròn...). Một cách hiểu hợp lý khác vẫn bị tính là sai.
3. **Metric phạt câu sai** (+1 / 0 / −1), nên biết khi nào *không* trả lời quan trọng ngang với trả lời đúng.

Đề xuất của nhóm:
- Giữ hướng **LLM sinh chương trình** trên các bảng đã chuẩn hoá.
- Đưa toàn bộ định nghĩa nghiệp vụ vào một **semantic layer có kiểm thử**: các hàm tất định, mỗi hàm có định nghĩa, kiểu, đơn vị và test so với đáp án mẫu.
- Bao quanh LLM bằng **tiền xử lý câu hỏi** và **kiểm tra kết quả** để biến câu sai thành câu bỏ qua.
- Ngoài prompting, xem xét **fine-tune bộ sinh mã** và một **mô hình quyết định trả lời / bỏ qua** học từ dữ liệu train.

Thí nghiệm được xếp theo mức ưu tiên ở mục 5.

---

## 1. Bài toán

### 1.1 Đầu vào và đầu ra

Mỗi câu hỏi đi kèm:
- `business_id`, tức thư mục dữ liệu của một cửa hàng (vài file CSV / XLSX / SQLite và một README mô tả dữ liệu);
- `as_of_date`: "hôm nay" của câu hỏi. File có thể chứa bản ghi sau ngày này, và những bản ghi đó coi như chưa xảy ra;
- cho cả bộ dữ liệu: một `rulebook.md` (quy ước nghiệp vụ) và một `calendar.csv` (lịch dương–âm, ngày lễ).

Ví dụ (minh hoạ):

| câu hỏi | đáp án mong đợi |
|---|---|
| `dthu th3/25 bnhiu z` | `{"type": "number", "answer": 184250000}` |
| `Tỉ lệ đơn bị huỷ tháng 5/2026 là bao nhiêu %?` | `{"type": "percent", "answer": 3.16}` |
| `Top 3 sp bán chạy nhất tuần trước` | `{"type": "list", "answer": ["Bia Tiger lon", "Coca 330ml", "Mì Hảo Hảo"]}` |
| `Tháng nào trong quý 1/2026 doanh thu cao nhất?` | `{"type": "date", "answer": "2026-02"}` |
| `Khách nữ mua bao nhiêu tiền tháng trước?` (cửa hàng không ghi giới tính) | `{"type": "unanswerable", "reason": "..."}` |

Các kiểu đáp án: number, percent, string, date, boolean, list, table, unanswerable, và **abstain** (bỏ qua). Đáp án phải đúng kiểu và khớp chính xác:
- tiền là số nguyên VND;
- percent làm tròn 2 chữ số;
- tên phải đúng như trong dữ liệu, kể cả dấu.

### 1.2 Khác gì so với Text-to-SQL thông thường

| | Text-to-SQL (Spider, BIRD) | DataMate |
|---|---|---|
| Dữ liệu | một DB sạch, có schema, khoá | file thô xuất từ phần mềm hoặc ghi tay; tiêu đề nhiều dòng, mỗi tháng một sheet, tiền dạng `55k`, ngày trộn nhiều định dạng |
| Schema | cho sẵn | phải tự khôi phục: cột nào là mã đơn, số lượng, trạng thái... |
| Ngữ nghĩa | câu hỏi gần như đủ nghĩa; BIRD có thêm "evidence" | câu hỏi rất thiếu; phần nghĩa còn lại nằm trong rulebook dài khoảng 20 nghìn ký tự |
| Đánh giá | execution accuracy | khớp chính xác với giá trị tham chiếu, sai bị **−1** |
| Câu hỏi | tiếng Anh chuẩn | tiếng Việt không dấu, viết tắt, teen-code, phương ngữ |
| Phân phối test | cùng miền | tập private có **ngành hàng chưa từng thấy** và rulebook riêng |

### 1.3 Metric và ràng buộc

**Penalized Accuracy:** PA = (số câu đúng − số câu sai) / N; câu bỏ qua được 0 điểm.

Nếu xác suất đúng của một câu là p, trả lời câu đó có giá trị kỳ vọng 2p − 1. Vì vậy chỉ nên trả lời khi p > 0,5. Ví dụ:
- precision 80%: mỗi câu trả lời đáng +0,6;
- precision 55%: chỉ đáng +0,1.

**Precision quan trọng hơn coverage.**

Ràng buộc của cuộc thi:
- chỉ dùng model mở trọng số phát hành trước 2026-10-01, **tổng ≤ 15 tỉ tham số cho mọi model dùng khi suy luận**;
- chạy **offline** trong Docker trên 2× RTX 5090;
- xử lý toàn bộ tập private trong **≤ 6 giờ**, kể cả nạp model;
- ban tổ chức chạy lại và kết quả phải lệch |ΔPA| ≤ 0,03, nên pipeline phải tất định;
- được sinh và chạy mã trong container;
- chỉ được huấn luyện trên train, dữ liệu tổng hợp hoặc dữ liệu công khai; **không được huấn luyện trên test**;
- không có logic gắn với ID câu hỏi hay cửa hàng.

---

## 2. Dữ liệu: những đặc điểm dẫn đến lựa chọn thiết kế

Các bộ dữ liệu:
- **train:** 30 cửa hàng, 3000 câu, có đáp án;
- **public test:** 14 cửa hàng, 1000 câu, không có đáp án;
- **private test:** chưa biết, có ngành mới.

Mục này chỉ nêu những đặc điểm *có ảnh hưởng tới thiết kế*; mỗi đặc điểm kèm quyết định mà nó dẫn tới (→).

### 2.1 File dữ liệu

**D1. Cùng một loại dữ liệu xuất hiện dưới nhiều "họ định dạng".** Ở train có khoảng 9 họ:
- phần mềm kiểu KiotViet (bảng hoá đơn và bảng chi tiết nối bằng mã);
- kiểu Sapo (mỗi dòng một mặt hàng; thông tin đơn chỉ ở dòng đầu);
- "Sổ chi tiết bán hàng" có dòng tiêu đề;
- sổ ghi tay tiêu đề hai dòng, mỗi tháng một sheet;
- CSV phẳng; CSV quan hệ; file chỉ có hoá đơn mà không có dòng hàng;
- SQLite của máy POS (17 bảng);
- nguồn hỗn hợp (phần mềm cộng sổ tay).

Ở public, 9/14 cửa hàng thuộc họ đã gặp, 5 cửa hàng thuộc họ hoặc ngành mới (điện thoại, sửa xe, mẹ và bé).

→ Tách bài toán thành hai tầng. **Tầng ingestion tất định** đưa mọi họ về một **canonical schema**. **Tầng hỏi đáp** chỉ làm việc trên schema đó. Đánh giá tổng quát hoá phải tính theo **họ định dạng và ngành**, không theo từng cửa hàng.

**D2. Bẩn có chủ ý, ở mức giá trị.** Ví dụ:
- tên cột viết tắt hoặc trộn Anh–Việt (`T.Tiền`, `Slg`, `Amount`);
- tiền dạng `55k`, `1,6256tr`, `55.000đ`; số lượng kèm đơn vị (`3 chai`), cần quy đổi về đơn vị gốc theo bảng quy đổi;
- ngày trộn định dạng trong một cột (`01/02/2025`, số serial Excel, `1 thg 1 2025`);
- dòng tiếp theo của một đơn bỏ trống, nghĩa là "giống dòng trên"; dòng trùng trong cùng một đơn là lỗi xuất file, chỉ tính một lần;
- trạng thái đơn có khi chỉ nằm trong cột ghi chú.

→ Các lỗi này **không nên để LLM xử lý** ở mỗi câu hỏi, vì nó sẽ làm sai không nhất quán. Ingestion giải quyết chúng một lần cho mỗi cửa hàng rồi cache lại.

**D3. Dữ liệu nhỏ, nhưng khả năng thì rất khác nhau giữa các cửa hàng.**
- Mỗi cửa hàng có 6 nghìn – 306 nghìn dòng, phủ 12–20 tháng; từ 1 đến 17 bảng.
- Tồn kho, nhập hàng, lô thuốc, khách hàng, công nợ, nhân viên, quy đổi đơn vị: mỗi thứ chỉ có ở một phần các cửa hàng. Có cửa hàng không có thứ nào, có cửa hàng có tất cả.
- Có cửa hàng không có dòng hàng, chỉ có tổng tiền mỗi hoá đơn.

→ Dữ liệu đủ nhỏ để nằm gọn trong bộ nhớ, nên **pandas trong một sandbox** là đủ và không cần hệ quản trị CSDL.

→ **Câu hỏi có trả lời được hay không phụ thuộc vào schema, không vào câu chữ.** Cùng một mẫu câu có thể trả lời được ở cửa hàng này mà không ở cửa hàng kia. Vì vậy quyết định "unanswerable" phải dựa trên việc dữ liệu có trường cần thiết và có phủ kỳ được hỏi hay không.

**D4. File chứa bản ghi sau `as_of_date`.** 75–79% câu có as-of bằng ngày cuối dữ liệu, số còn lại lùi tới khoảng 1,5 năm.

→ Việc cắt theo as-of phải nằm **trong hàm dùng chung**, không để LLM tự nhớ. Lỗi này lại bị che trên tập dev nhỏ, vì phần lớn câu không cần cắt.

### 2.2 Rulebook

**D5. Phần lớn "nghĩa" của câu hỏi nằm trong rulebook, và các quy tắc khá trái trực giác.** Một số quy tắc mặc định:
- doanh thu = tổng tiền dòng sau chiết khấu dòng; **không** trừ voucher cấp đơn, không tính VAT và phí ship;
- **loại đơn huỷ và cả đơn có bất kỳ dòng nào bị trả**;
- đơn được gán theo ngày đặt;
- "bán chạy" tính theo số lượng quy đổi về đơn vị gốc;
- "tuần trước" là tuần đầy đủ từ thứ Hai đến Chủ nhật;
- "tháng này" so với *cùng số ngày* của tháng trước;
- Tết là từ 23 tháng Chạp đến mùng 10 tháng Giêng;
- giá trị trung bình một đơn làm tròn half-up đến đồng;
- khách lẻ bị loại khỏi xếp hạng khách;
- hoà thì xếp theo mã tăng dần.

Một câu như "doanh thu tháng 3" có thể sai theo khoảng 6 cách khác nhau mà kết quả vẫn trông hợp lý.

→ Các định nghĩa dùng chung được **cài một lần, kiểm thử với đáp án train**, và đưa cho LLM dưới dạng hàm (semantic layer, mục 4.3).

**D6. Rulebook khác nhau giữa các bộ dữ liệu.** Rulebook public (23,8 nghìn ký tự) thêm các phần cho ngành điện thoại, sửa xe, mẹ và bé, cùng các định nghĩa "nhất" (khách trung thành nhất, ngày đắt khách nhất...), và bỏ một vài quy tắc của train. Rulebook private sẽ có quy tắc chưa ai thấy.

→ Rulebook **luôn nằm trong prompt** để LLM đọc lúc suy luận. Semantic layer chỉ chứa các định nghĩa *ổn định*, và mỗi hàm gắn với mệnh đề rulebook tương ứng để có thể tắt khi mệnh đề đó thay đổi.

→ **Không** xây "tool theo intent" (ví dụ `cancel_rate()`, `top_products()`). Các tool như vậy mã hoá cứng cả cách hiểu câu hỏi lẫn quy tắc của train.

### 2.3 Câu hỏi

**D7. Câu hỏi rất nhiễu.** Trên 3000 câu train:
- 25,8% viết không dấu;
- viết tắt và teen-code: `dthu` (646 câu), `bnhiu` (396), `bn` (222), `z` (155), `dc/đc` (274);
- dư phím Telex khi gõ sai (`nhaats`, `cuwar`): khoảng 100 câu;
- trộn tiếng Anh (`revenue`, `bill`, `top`, `hot`): khoảng 7–8%;
- ngày và tiền viết nén (`th7/26`, `3/25`, `6-2025`, `3tr7`, `152k`): khoảng 780 câu;
- tập public còn có phương ngữ (`ni`, `rứa`, `mô`, `chừ`).

Tên mặt hàng hoặc khách thường viết không dấu và phải khớp đúng tên trong danh mục: 678 câu.

→ Cần một bước **chuẩn hoá câu hỏi** trước khi sinh mã. Phần lớn làm được bằng quy tắc, vừa rẻ vừa tất định; một lần gọi LLM ngắn xử lý phần còn lại.

**D8. Kỳ thời gian là tham số quan trọng nhất và hay bị hiểu sai.** 13% câu có từ 2 kỳ trở lên ("tháng 3 so với tháng 2", "từ tháng 1 tới tháng 3, tháng sau có cao hơn tháng trước không"). Một số cách viết dễ đọc nhầm: `5/25` là tháng 5/2025 chứ không phải ngày 25/5; `quý 2/2025` không phải tháng 2.

→ Chuyển kỳ thời gian sang lời gọi hàm tất định, ví dụ `month(2025, 3)` hay `quarter(2025, 2)`, rồi đưa cho LLM ngay cạnh câu hỏi. Đây là hướng tương tự "evidence" của BIRD.

**D9. Câu hỏi public ít trùng bề mặt với train.** Chỉ 4,2% câu public khớp nguyên văn một khung câu train (sau khi bỏ dấu và che số). Intent thì lặp lại, nhưng cách diễn đạt và tỉ trọng intent thay đổi: public có câu về khách gấp khoảng 2 lần, câu về lợi nhuận gấp khoảng 7 lần.

→ Ví dụ few-shot nên truy xuất theo **câu đã chuẩn hoá**. Hệ thống phải tổng quát hoá theo *intent*, không theo *mẫu câu*. Đây cũng là lập luận chống lại các tool theo intent cố định.

### 2.4 Đáp án

**D10. 82% đáp án là số** (number 62,8%, percent 19,3%), và phải khớp chính xác. Phân bố các kiểu còn lại: list 193, date 145, string 88, unanswerable 70 (2,3%), boolean 23, table 19.

→ Trọng tâm là **tính toán chính xác theo định nghĩa**, không phải sinh văn bản.

**D11. Đáp án có những quy luật rất mạnh** (đo trên đáp án train):
- **chỉ 2/1884 đáp án number bằng 0**, nên một đáp án 0 gần như chắc chắn là lỗi lọc rỗng;
- mọi đáp án number đều là số nguyên;
- gợi ý trong câu hỏi gần như quyết định kiểu đáp án: "tháng nào" → `YYYY-MM`, "%" hoặc "tỉ lệ" → percent, "top N" → list có ≤ N phần tử. Các gợi ý này đúng **100% trên 808 câu**;
- đáp án dạng ngày luôn nằm trong kỳ được hỏi (141/141);
- tỉ lệ nằm trong [0, 100];
- số đếm là số nguyên.

→ Các quy luật này dùng được làm **bộ kiểm tra kết quả không cần dữ liệu** (mục 4.6). Cái giá báo nhầm gần như bằng 0.

**D12. Unanswerable có nguyên nhân rõ ràng:**
- thiếu trường (giới tính, địa chỉ, lý do huỷ, không có file tồn kho...): khoảng 45/70;
- kỳ nằm ngoài dữ liệu: khoảng 16;
- câu hỏi chủ quan ("nhân viên tận tâm nhất"): khoảng 8;
- cần kiến thức ngoài: 1.

Câu có kỳ giao một phần với dữ liệu **vẫn phải trả lời**, tính trên phần có dữ liệu.

→ Chỉ tuyên bố unanswerable khi có bằng chứng tất định (thiếu trường, kỳ ngoài dữ liệu). Khi không chắc thì **abstain** chứ không đoán unanswerable.

### 2.5 Tóm tắt: đặc điểm → quyết định

| đặc điểm | quyết định |
|---|---|
| D1–D3: nhiều họ định dạng, bẩn ở mức giá trị, dữ liệu nhỏ | ingestion tất định → canonical schema; pandas trong sandbox |
| D4–D6: nghĩa nằm trong rulebook; rulebook thay đổi | semantic layer (hàm định nghĩa có kiểm thử) + rulebook luôn trong prompt; không dùng tool theo intent |
| D7–D9: câu hỏi nhiễu, kỳ thời gian khó, ít trùng train | chuẩn hoá câu hỏi (quy tắc + 1 lần LLM); kỳ → lời gọi hàm tất định |
| D10–D12: đáp án số chính xác, quy luật mạnh, unanswerable có nguyên nhân | kiểm tra kết quả hai tầng; finalize đúng format; abstain khi nghi ngờ |

---

## 3. Các hướng tiếp cận và lựa chọn

### 3.1 Các hướng dựa trên prompting

| hướng | mô tả | đánh giá theo dữ liệu | quyết định |
|---|---|---|---|
| **A. LLM sinh mã pandas** | LLM đọc rulebook, schema và câu hỏi, viết chương trình Python ngắn, chạy trong sandbox | tổng quát nhất: intent mới hay ngành mới vẫn viết được mã. Điểm yếu là logic nghiệp vụ nằm trong mã sinh ra, nên khó kiểm soát và khó debug | **giữ làm khung**; bù điểm yếu bằng semantic layer và kiểm tra kết quả |
| **B. Tool theo intent** | viết sẵn hàm cho từng loại câu (`cancel_rate`, `top_products`...); LLM chỉ chọn tool và điền tham số | precision cao trên intent đã biết, nhưng gãy với intent và quy tắc mới (D6, D9). Một tool áp quy tắc cũ sẽ **sai một cách tự tin** (−1) | **bỏ**; thay bằng *primitive* cấp thấp hơn mà LLM ghép lại |
| **C. Text-to-SQL bằng prompting** | LLM viết SQL trên các bảng đã chuẩn hoá | SQL không giải quyết hai nguồn lỗi chính là ingestion (D2) và định nghĩa (D5). Các bước nghiệp vụ như quy đổi đơn vị, kỳ âm lịch, làm tròn half-up hay tie-break theo mã viết bằng SQL dài và dễ sai hơn gọi một hàm Python | không ưu tiên khi chỉ dùng prompting (xem 3.3 cho trường hợp fine-tune) |
| **D. Agent nhiều bước** | LLM điều phối nhiều công cụ và nhiều vòng | nhiều lời gọi LLM mỗi câu, trong khi ngân sách chỉ khoảng 7 giây mỗi câu nếu private có khoảng 3000 câu; khó tái lập (\|ΔPA\| ≤ 0,03); model khoảng 9B điều phối kém | **bỏ** |

### 3.2 Các hướng ngoài prompting

Cuộc thi cho phép huấn luyện trên train, dữ liệu tổng hợp và dữ liệu công khai. Có ba hướng đáng cân nhắc, xếp theo tiềm năng:

1. **Fine-tune bộ sinh mã** (SQL hoặc Python) trên dữ liệu sinh từ train. Phân tích chi tiết ở 3.3.
2. **Mô hình quyết định trả lời / bỏ qua.** Học P(đúng) từ các đặc trưng có sẵn sau mỗi câu, rồi chọn ngưỡng tối đa hoá PA. Các đặc trưng gồm:
   - kiểu đáp án và nhóm intent;
   - câu hỏi có đi qua kiểm tra nào không, có phải sửa lỗi không;
   - kết quả 0 hoặc rỗng;
   - các mẫu có đồng thuận không;
   - cửa hàng thuộc họ định dạng đã gặp hay chưa.

   Mô hình rất nhỏ (logistic regression hoặc cây quyết định) nên gần như không tốn ngân sách tham số. Nó tấn công trực tiếp vào điểm yếu "precision ≈ ngưỡng hoà vốn". Dữ liệu huấn luyện là kết quả chạy toàn bộ train (thí nghiệm E0).
3. **Học gán vai trò cột cho layout mới.** Một bộ phân loại nhỏ dựa trên tên cột, giá trị mẫu và README giúp ingestion khi gặp từ vựng lạ (ngành mới ở private). Hướng này để sau; chỉ làm nếu E0 cho thấy lỗi ingestion chiếm tỉ trọng đáng kể.

### 3.3 Fine-tune Text-to-SQL có cải thiện kết quả không?

**Có thể huấn luyện được không.** Train có 3000 câu kèm đáp án nhưng **không có chương trình mẫu** (gold SQL hay code). Có thể tạo dữ liệu huấn luyện theo kiểu *rejection-sampling fine-tuning* (STaR / RFT):
1. cho model hiện tại sinh nhiều chương trình cho mỗi câu train;
2. chạy từng chương trình;
3. giữ lại những chương trình cho kết quả **khớp đáp án**;
4. dùng các cặp (prompt, chương trình đúng) để fine-tune LoRA cho chính model đó.

Dữ liệu như vậy hợp lệ (sinh từ train) và không cần model thầy bên ngoài. Có thể bổ sung chương trình từ một model mở lớn hơn chạy offline lúc huấn luyện. Ràng buộc 15B chỉ áp dụng khi suy luận, nhưng điều này cần xác nhận lại với luật.

**Kỳ vọng cải thiện ở đâu**

| loại lỗi | fine-tune có giúp không | lý do |
|---|---|---|
| Lỗi sinh mã (sai tên cột, sai kiểu, dùng sai hàm, quên lọc as-of) | **có, nhiều** | model học đúng "phương ngữ" của API và canonical schema; đây là chỗ fine-tune mạnh nhất trong các báo cáo Text-to-SQL với model 7B–14B (CodeS, OmniSQL) |
| Định nghĩa nghiệp vụ của train (D5) | **có, trên các họ đã gặp** | model học thuộc các mặc định của rulebook train. 9/14 cửa hàng public thuộc họ đã gặp, nên có thể có lợi đáng kể ở public |
| Quy tắc hoặc ngành mới ở private (D6) | **không chắc, có thể hại** | model đã fine-tune có xu hướng làm theo thói quen đã học thay vì đọc rulebook mới trong prompt (xung đột tri thức) |
| Tốc độ | **có** | model đã fine-tune cần ít ví dụ few-shot hơn, nên prompt ngắn hơn và nhanh hơn. Điều này quan trọng với ngân sách 6 giờ |
| Quyết định trả lời / bỏ qua | ít | đây là bài toán hiệu chỉnh xác suất, giải quyết tốt hơn bằng hướng 2 ở mục 3.2 |

**SQL hay Python làm ngôn ngữ đích**

| | SQL | Python với semantic layer |
|---|---|---|
| Dữ liệu huấn luyện bổ sung | có các corpus Text-to-SQL công khai (Spider, BIRD) và model đã fine-tune sẵn | chỉ có dữ liệu tự sinh từ train |
| Các phép nghiệp vụ (quy đổi đơn vị, kỳ âm lịch, làm tròn half-up, tie-break) | phải cài thành view hoặc macro trên một SQL engine nhúng chạy trên bảng đã chuẩn hoá | đã có sẵn dưới dạng hàm Python |
| Khoảng cách miền | lớn: corpus công khai là tiếng Anh với DB sạch, khác nhiều với câu hỏi tiếng Việt nhiễu và dữ liệu thô | nhỏ |
| Pipeline hiện có | phải viết lại phần thực thi và kiểm tra | dùng lại nguyên vẹn |

**Đánh giá:**
- Fine-tune nhiều khả năng **giảm lỗi sinh mã và tăng tốc độ**, và **tăng độ chính xác trên các họ định dạng đã gặp**.
- Với ngành và quy tắc mới, lợi ích chưa chắc chắn, thậm chí có thể âm.
- Lợi thế duy nhất của SQL là các corpus công khai. Corpus này lệch miền khá xa, nên nhóm đề xuất fine-tune trên **cùng không gian chương trình Python và semantic layer** trước, để tách riêng tác động của fine-tune khỏi tác động của việc đổi ngôn ngữ.

Hai điều kiện để có kết luận đúng:
1. **Đánh giá leave-family-out:** huấn luyện trên một số họ định dạng, đo trên các họ còn lại, để mô phỏng private.
2. **Tăng cường bằng biến thể rulebook:** sửa một quy tắc trong rulebook (ví dụ "tính cả đơn có hàng trả"), tính lại đáp án bằng semantic layer với định nghĩa tương ứng, rồi đưa vào dữ liệu huấn luyện. Mục đích là dạy model *đọc* rulebook thay vì học thuộc.

Vì dữ liệu huấn luyện phụ thuộc vào API của semantic layer, fine-tune chỉ nên làm **sau khi semantic layer ổn định** (thí nghiệm E5, mục 5).

### 3.4 Lựa chọn

**Khung chính:** A (LLM sinh mã Python) + semantic layer có kiểm thử + kiểm tra kết quả + mô hình quyết định trả lời / bỏ qua.

**Hướng mở rộng:** fine-tune bộ sinh mã. Không dùng B (tool theo intent) và D (agent). C chỉ đáng xét lại nếu fine-tune cho thấy lợi ích và cần tận dụng corpus SQL công khai.

---

## 4. Giải pháp đề xuất

### 4.1 Tổng quan

```
 File thô của cửa hàng (CSV · XLSX · SQLite · README)
        │
        ▼
 ① Ingestion ─────────► ② Canonical schema
   (một lần / cửa hàng,     sales_lines · orders · các bảng đã làm sạch
    tất định, cache)        (sản phẩm, khách, nhập, tồn, lô, công nợ…)
                                   │
                                   ▼
                            ③ Semantic layer
                              hàm kỳ thời gian · hàm định nghĩa (doanh thu,
                              trung bình đơn…) · hàm xếp hạng / tìm tên ·
                              hàm xuất đáp án (kiểu, làm tròn)
                                   │
 Câu hỏi ──► ④ Chuẩn hoá câu hỏi ──┤
             (quy tắc + 1 lần LLM)  │
             → câu chuẩn, kỳ thời   │
               gian, tên, kỳ vọng   ▼
               về đáp án     ⑤ LLM sinh chương trình ──► ⑥ Sandbox chạy
                                   ▲                          │
                                   │  lỗi chạy, hoặc đáp án   ▼
                                   └── mâu thuẫn (1 vòng) ◄─ ⑦ Kiểm tra kết quả
                                                              │
                                                              ▼
                                                     ⑧ Finalize + quyết định
                                                       đáp án │ unanswerable │ abstain
```

### 4.2 Ví dụ xuyên suốt (minh hoạ)

Câu hỏi: `dthu tb moi don th3/25 bnhiu z`, với `as_of_date` = 2025-06-30.

| bước | kết quả |
|---|---|
| ④ quy tắc | `Doanh thu trung bình mỗi đơn tháng 3/2025 bao nhiêu vậy` (`dthu` → doanh thu, `tb` → trung bình, `th3/25` → tháng 3/2025, `bnhiu` → bao nhiêu, `z` → vậy) |
| ④ LLM | `{"normalized": "...", "answer_type": "number", "top_k": null, "sub_questions": [...]}` |
| ④ kỳ thời gian | `"tháng 3/2025" -> month(2025, 3) = 2025-03-01..2025-03-31` |
| ④ kỳ vọng | đáp án kiểu number, là tiền VND |
| ⑤ LLM sinh | `answer_number(avg_order_value(month(2025, 3)), kind='money')` |
| ⑥ chạy | 187 500 |
| ⑦ kiểm tra | đúng kiểu; số nguyên; ≥ 1000 đ; tính lại độc lập bằng `avg_order_value(month(2025, 3))` cũng ra 187 500, nên đồng thuận |
| ⑧ finalize | `{"type": "number", "answer": 187500}` |

Nếu LLM tự viết `doanh thu / số dòng hàng` thay vì `/ số đơn`, kết quả sẽ lệch khoảng ×2 mà vẫn "trông hợp lý". Bước ⑦ phát hiện ra sự khác biệt này nhờ tính lại bằng hàm định nghĩa, và gửi lại cho LLM một lần (xem 4.7).

### 4.3 Ingestion và canonical schema

**Ingestion** (đã có) làm các việc sau cho mỗi cửa hàng, rồi cache kết quả:
- tìm dòng tiêu đề thật, bỏ dòng tiêu đề phụ, dòng lọc và dòng trống;
- gộp các sheet theo tháng và các file bị chia nhỏ;
- đọc tiền, số lượng kèm đơn vị và ngày trộn định dạng;
- gán **vai trò** cho từng cột (mã đơn, ngày, sản phẩm, số lượng, đơn vị, thành tiền, khách, trạng thái...) dựa trên từ vựng của README và tên cột;
- điền lại ô trống ở dòng tiếp theo của một đơn; bỏ dòng trùng trong một đơn;
- chuẩn hoá trạng thái về một bộ nhãn chung (hoàn thành, huỷ, trả hàng, đang giao...).

**Canonical schema** là một vài bảng có tên cột và ý nghĩa cố định, *giống nhau ở mọi cửa hàng*, để LLM và các hàm định nghĩa không phải đoán.

| bảng | một dòng là | các cột chính |
|---|---|---|
| `sales_lines()` | một mặt hàng trong một đơn bán | `order_id`, `date` (ngày đặt), `product_id`, `product_name`, `qty` / `qty_base` (số lượng, và số lượng quy về đơn vị gốc), `amount` (thành tiền sau chiết khấu dòng), `customer_id` / `customer_name`, `walkin` (khách lẻ), `status`, `counted` (dòng có được tính vào doanh thu theo rulebook: đơn không huỷ, không có dòng trả, là hàng hoá chứ không phải phí hay voucher), cộng các thuộc tính của đơn (kênh, hình thức thanh toán) và của sản phẩm (nhóm, giá vốn, hoạt chất...) |
| `orders()` | một đơn | `order_id`, `date`, `status`, khách, `goods_amount` (tiền hàng của đơn), `counted` |
| các bảng khác | tuỳ cửa hàng | sản phẩm, khách, phiếu nhập, tồn kho, lô thuốc, công nợ, nhân viên, ca: là bảng gốc đã làm sạch và gán vai trò cột. Dự kiến chuẩn hoá thêm các bảng `customers`, `stock`, `debt` khi có hàm định nghĩa tương ứng |

Ingestion cũng sinh một **schema card** cho mỗi cửa hàng: danh sách bảng, cột, vai trò và giá trị mẫu. Schema card được đưa vào prompt.

### 4.4 Semantic layer

**Primitive là gì.** Primitive là một hàm tất định trả về *một đại lượng nghiệp vụ hoặc một tập dữ liệu đã áp định nghĩa*, không phải đáp án của một loại câu hỏi. LLM vẫn tự lọc, nhóm, xếp hạng và định dạng, nhưng không phải tự cài lại các định nghĩa.

| nhóm | hàm (ví dụ) | ý nghĩa |
|---|---|---|
| Kỳ thời gian | `month(y, m)`, `quarter(y, q)`, `week_prev()`, `this_month()`, `prev_month_same_days()`, `ytd()`, `last_n_days(n)`, `tet(y)`, `prev_period(p)`, `same_period_last_year(p)`, `in_period(dates, p)` | trả về một khoảng ngày, **đã cắt theo as-of**, theo đúng quy ước của rulebook (tuần từ thứ Hai, Tết theo lịch âm...) |
| Dữ liệu đã định nghĩa | `sales_lines()`, `orders()` | các bảng canonical ở 4.3 |
| Đại lượng | `revenue(p)`, `avg_order_value(p)`, `price_on(product, day)` | doanh thu theo định nghĩa mặc định; doanh thu / số đơn được tính, làm tròn half-up; giá bán tại một ngày |
| Tiện ích | `rank_top(...)`, `find(name)`, `is_walkin(...)`, `pct_change(a, b)`, `round_half_up(x, n)` | xếp hạng với tie-break theo mã; tìm tên trong danh mục; nhận diện khách lẻ; phần trăm thay đổi |
| Xuất đáp án | `answer_number(x, kind='money')`, `answer_percent`, `answer_list`, `answer_table`, `answer_month`, `unanswerable(reason)` | ép đúng kiểu, làm tròn và format |

**Mỗi primitive là một hợp đồng.** Một primitive phải có:
1. định nghĩa nghiệp vụ viết rõ, trích từ rulebook;
2. quy tắc đặt ở một chỗ duy nhất;
3. kiểu và đơn vị rõ ràng (tiền là số nguyên VND, percent đã nhân 100);
4. **kiểm thử hợp đồng**: chọn các câu train hỏi *đúng* đại lượng đó trong *đúng một* kỳ, tính bằng hàm và so với đáp án;
5. **số version**: mọi thay đổi định nghĩa phải tăng version, và kết quả chạy ghi lại version đã dùng.

Kết quả kiểm thử hợp đồng hiện tại, chỉ dùng hàm và đáp án train (không có LLM):

| hợp đồng | số câu kiểm được | khớp đáp án |
|---|---|---|
| giá trị trung bình một đơn | 167 | 167 |
| doanh thu | 259 | 259 |
| số đơn | 136 | 136 |
| số đơn huỷ | 100 | 100 |
| tỉ lệ huỷ | 104 | 104 |

Các primitive dự kiến tiếp theo:
- doanh số theo khách (loại khách lẻ, định danh theo mã, gộp biến thể tên);
- số lượng bán theo đơn vị gốc;
- công nợ tại một ngày và phân nhóm tuổi nợ;
- lợi nhuận gộp (giá vốn ghi trên dòng bán);
- tồn kho cuối ngày.

### 4.5 Chuẩn hoá câu hỏi

Bước này chạy **quy tắc tất định trước**, theo thứ tự sau:
1. **Telex dư:** `nhaats` → nhất, `cuwar` → cửa. Một từ chỉ được giải khi kết quả là âm tiết tiếng Việt hợp lệ (âm đầu + vần), nên `check`, `plus` hay tên thương hiệu không bị đổi.
2. **Tên trong danh mục:** tìm tên sản phẩm, khách, nhân viên hay nhóm hàng của cửa hàng trong câu, có tôn trọng dấu (`tháng` không bị nhận nhầm thành tên `Thắng`). Tên khớp chính xác được thay bằng cách viết trong dữ liệu và được khoá lại để các bước sau không sửa.
3. **Từ điển** viết tắt, teen-code, phương ngữ và tiếng Anh: `dthu`, `bnhiu`, `z`, `mik`, `mô`, `ni`, `revenue`. Các từ mơ hồ chỉ được đổi theo ngữ cảnh, ví dụ `k` sau một con số là nghìn, còn lại là "không".
4. **Tiền và ngày:** `3tr7` → 3.700.000 đồng; `th7/26` → tháng 7/2026; `quý 2/25` → quý 2 năm 2025.

Sau đó có **một lần gọi LLM** (greedy, ≤ 400 token) trả về JSON: câu chuẩn hoá, kiểu đáp án dự đoán, k của top-k, các câu hỏi con và các tên nhắc tới. Kết quả bị **loại** (quay về bản đã qua quy tắc) nếu LLM làm đổi một con số, mất một tên chính xác, hoặc bỏ một từ thuộc tên trong danh mục.

LLM sinh mã ở bước sau thấy **cả câu gốc và câu chuẩn hoá**; nếu mâu thuẫn, câu gốc thắng. Kỳ thời gian được giải từ bản đã qua quy tắc thành các lời gọi hàm (D8).

### 4.6 Sinh mã và sandbox

Prompt gồm:
- rulebook;
- tài liệu API của semantic layer;
- README và schema card của cửa hàng;
- 2–4 ví dụ train gần nhất (không lấy từ cùng cửa hàng);
- câu hỏi gốc và câu chuẩn hoá, kỳ thời gian và kỳ vọng về đáp án.

LLM (hiện là Qwen3.5-9B, greedy) viết một chương trình ngắn kết thúc bằng một hàm `answer_*()` hoặc `unanswerable()`. Chương trình chạy trong một tiến trình riêng, có sẵn bảng trong bộ nhớ, có timeout và giới hạn RAM. Nếu chương trình lỗi, LLM được sửa **một lần** với traceback.

### 4.7 Kiểm tra kết quả

Hai khái niệm cần phân biệt:
- **Kiểm tra cứng:** quy tắc đúng gần 100% trên đáp án train. Nếu đáp án vi phạm thì gần như chắc chắn sai. Hệ thống gửi cho LLM chương trình cũ kèm thông báo vi phạm và cho sửa **một vòng**. Nếu vẫn vi phạm thì **abstain**, tức nhận 0 thay vì −1.
- **Kiểm tra mềm:** chỉ là gợi ý cho LLM, không ép abstain. Ví dụ, kiểu đáp án do LLM ở bước ④ đoán là kiểm tra mềm, vì bản thân nó có thể sai.

Có hai tầng kiểm tra:

**Tầng 1: kiểm tra cấu trúc.** Tầng này xét *hình dạng* của đáp án, không cần tính lại từ dữ liệu.

| kiểm tra | ví dụ vi phạm | cơ sở |
|---|---|---|
| Kiểu | câu hỏi "tỉ lệ huỷ bao nhiêu %" nhưng đáp án là number | gợi ý kiểu đúng 100% trên 808 câu (D11) |
| Tháng hay ngày | "tháng nào" nhưng trả `2025-03-15` | như trên |
| Kỳ | "ngày nào trong tháng 10" nhưng trả `2025-11-01` | đáp án ngày luôn trong kỳ hỏi (141/141) |
| k | "top 3" nhưng trả 4 phần tử | 120/120 |
| Bất biến | tỉ lệ ngoài [0, 100]; số đếm không nguyên; số ngày dài hơn kỳ; số âm khi câu hỏi không về chênh lệch hay lãi lỗ; tiền < 1000 đ (sai đơn vị); khách lẻ trong xếp hạng khách | 0 báo nhầm trên 2930 đáp án train |

**Tầng 2: kiểm tra ngữ nghĩa (tính lại bằng primitive).** Tầng cấu trúc không bắt được lỗi định nghĩa. Ví dụ, tỉ lệ huỷ 44,94% thay vì 3,16% (do map trạng thái sai) vẫn là một phần trăm hợp lệ về hình dạng.

Cách làm như sau. Khi câu hỏi hỏi *đúng một* đại lượng có hợp đồng (doanh thu, trung bình đơn, số đơn, số đơn huỷ, tỉ lệ huỷ) trong *đúng một* kỳ, hệ thống tính lại đại lượng đó bằng primitive. Nếu đáp án của LLM khác, LLM nhận một thông báo dạng "đáp án khác định nghĩa của rulebook; hãy tính lại bằng `avg_order_value(month(2025, 3))`" và được sửa một vòng. Nếu vẫn khác thì abstain.

Vì sao *kiểm tra* bằng primitive mà không *thay* đáp án bằng primitive:
- Bộ nhận diện "câu này hỏi đúng đại lượng X" có thể sai, ví dụ câu có thêm điều kiện lọc mà bộ nhận diện bỏ sót. Lúc đó chương trình của LLM mới là chương trình đúng.
- Dùng làm kiểm tra thì hai đường tính độc lập phải **đồng thuận**. Khi đồng thuận, độ tin cậy cao. Khi lệch, LLM có một cơ hội xem lại. Khi vẫn lệch, hệ thống bỏ qua, và cái giá là 0 chứ không phải −1.

### 4.8 Finalize và quyết định trả lời

**Finalize** bảo đảm đúng format:
- số nguyên khi giá trị nguyên;
- percent làm tròn 2 chữ số, −0 thành 0;
- ngày đúng `YYYY-MM-DD` hoặc `YYYY-MM`;
- bảng lưu theo cột thì chuyển vị;
- bất cứ thứ gì sai dạng → abstain.

Lỗi format không bao giờ được phép thành −1.

**Quyết định trả lời / bỏ qua:**
- hiện tại: abstain khi kiểm tra cứng thất bại sau vòng sửa, hoặc khi không chương trình nào chạy ra đáp án;
- unanswerable chỉ khi có bằng chứng tất định;
- dự kiến: mô hình P(đúng) và ngưỡng theo nhóm intent (thí nghiệm E4).

---

## 5. Thí nghiệm, theo mức ưu tiên

Nguyên tắc chung:
- mỗi thí nghiệm **chỉ đổi một yếu tố**; model, rulebook, bộ câu hỏi, cấu hình sinh và môi trường chạy giữ cố định;
- so sánh **theo cặp** trên cùng các câu, kèm khoảng tin cậy bootstrap 95% của ΔPA;
- các lượt chạy toàn bộ train làm trên Kaggle (2× T4).

| ưu tiên | thí nghiệm | câu hỏi cần trả lời | mức cần thiết |
|---|---|---|---|
| 1 | **E0: baseline toàn bộ train** | pipeline hiện tại sai ở đâu, theo kiểu đáp án, họ định dạng và intent? | **bắt buộc**: mọi thí nghiệm khác so với nó (đang chạy) |
| 2 | **E3: kiểm tra kết quả** | kiểm tra cấu trúc và ngữ nghĩa chuyển được bao nhiêu câu sai thành abstain, và làm mất bao nhiêu câu đúng? | **cao**: rẻ, tác động trực tiếp lên precision |
| 3 | **E2: mở rộng semantic layer** | primitive có kiểm thử giảm lỗi định nghĩa bao nhiêu? | **cao**: nhắm vào nguồn lỗi lớn nhất (D5) |
| 4 | **E4: mô hình quyết định trả lời / bỏ qua** | ngưỡng abstain học từ dữ liệu cải thiện PA bao nhiêu so với quy tắc cố định? | **cao**: chi phí thấp, dùng lại dữ liệu E0 |
| 5 | **E1: chuẩn hoá câu hỏi** | chuẩn hoá có giúp không; lần gọi LLM có đáng thời gian so với chỉ dùng quy tắc? | **trung bình**: quyết định giữ hay bỏ 2–3 giây mỗi câu |
| 6 | **E5: fine-tune bộ sinh mã** | fine-tune có tăng PA trên họ định dạng chưa gặp không? | **khám phá**: tiềm năng cao nhưng tốn kém, làm sau khi semantic layer ổn định |
| + | **Độ biến thiên** | hai lần chạy cùng cấu hình lệch nhau bao nhiêu? | **bắt buộc**, đi kèm E0: là sàn nhiễu để đọc mọi ΔPA, và liên quan trực tiếp tới quy định \|ΔPA\| ≤ 0,03 |

### E0: baseline trên toàn bộ train (đang chạy)

- **Là gì:** chạy pipeline hiện tại (chuẩn hoá đầy đủ, kiểm tra hai tầng, 1 mẫu greedy) trên cả 3000 câu train, rồi so với đáp án.
- **Vì sao:** tập dev 120 câu có sai số chuẩn của PA khoảng 0,07, quá lớn để so sánh các thay đổi. Toàn bộ train cho sai số khoảng 0,016 và đủ câu cho từng nhóm. E0 cung cấp:
  1. phân loại lỗi theo nhóm: định nghĩa, sinh mã, hiểu câu, ingestion;
  2. precision theo kiểu đáp án, họ định dạng và intent;
  3. dữ liệu huấn luyện cho E4 và dữ liệu khởi đầu cho E5.
- **Kết quả dùng để:** chọn primitive cho E2 theo số câu sai mà mỗi primitive sửa được; quyết định thứ tự của các thí nghiệm sau.
- **Đi kèm:** chạy lại đúng cấu hình đó thêm một lần để đo độ biến thiên.

### E3: kiểm tra kết quả

- **Là gì:** ba nhánh: (a) tắt kiểm tra, (b) chỉ tầng cấu trúc, (c) cấu trúc + ngữ nghĩa. Semantic layer giữ cố định.
- **Vì sao:** dưới metric có phạt, chuyển một câu sai thành abstain được +1, ngang với sửa đúng một câu. Các kiểm tra dựa trên quy luật D11 gần như không báo nhầm trên đáp án train, nhưng cần đo **trên đáp án do model sinh ra**, nơi báo nhầm có thể khác.
- **Đo:** số câu sai bị bắt (recall); số câu đúng bị chuyển nhầm thành abstain; ΔPA; chi phí thời gian của vòng sửa.
- **Giữ lại nếu:** báo nhầm ≤ 1% số câu đúng và ΔPA > 0.
- **Cần thêm:** cờ để bật hoặc tắt từng tầng. Hiện cả hai tầng luôn bật.

### E2: mở rộng semantic layer

- **Là gì:** thêm các primitive ở 4.4 (doanh số theo khách, số lượng theo đơn vị gốc, công nợ, lợi nhuận gộp, tồn kho), mỗi primitive có kiểm thử hợp đồng. So sánh semantic layer hiện tại với bản mở rộng, **tắt kiểm tra ngữ nghĩa** để không lẫn với E3.
- **Vì sao:** lỗi định nghĩa là nguồn lỗi chính dự kiến (D5). Một định nghĩa được cài một lần và kiểm thử sẽ đúng ở mọi câu dùng nó, thay vì phụ thuộc vào việc LLM nhớ đủ khoảng 6 quy tắc mỗi lần.
- **Đo:** (1) kiểm thử hợp đồng trên đáp án train (không cần LLM, chạy trong vài chục giây); (2) ΔPA và số câu sai ở các nhóm intent có primitive mới.
- **Giữ lại nếu:** kiểm thử hợp đồng khớp 100% trên các câu tính được, và ΔPA > 0 ở nhóm liên quan.
- **E2 và E3 cài song song nhưng đo tách riêng:** cả hai đều dùng primitive, và nếu đo chung sẽ không biết lợi ích đến từ việc LLM dùng hàm đúng hay từ việc kiểm tra bắt lỗi.

### E4: mô hình quyết định trả lời / bỏ qua

- **Là gì:** từ kết quả E0, học P(đúng) của mỗi câu bằng một mô hình nhỏ trên các đặc trưng ở 3.2, rồi chọn ngưỡng (có thể theo nhóm intent) để tối đa hoá PA. Đánh giá bằng kiểm định chéo theo cửa hàng hoặc theo họ định dạng.
- **Vì sao:** PA phụ thuộc trực tiếp vào precision. Một nhóm intent có precision 45% nên bỏ qua toàn bộ; một nhóm 90% nên luôn trả lời. Quy tắc cố định hiện tại không phân biệt được hai trường hợp này.
- **Đo:** PA, coverage và precision trên phần giữ lại; độ lệch hiệu chỉnh (calibration) khi chuyển sang họ chưa gặp.
- **Rủi ro:** mô hình học trên train sẽ tự tin quá mức trên ngành mới. Vì vậy dùng đặc trưng "họ đã gặp hay chưa" và đánh giá leave-family-out.

### E1: chuẩn hoá câu hỏi

- **Là gì:** ba nhánh: (a) tắt chuẩn hoá, (b) chỉ quy tắc, (c) quy tắc + 1 lần LLM.
- **Vì sao:** câu hỏi rất nhiễu (D7, D8), nhưng không rõ nhiễu chiếm bao nhiêu phần lỗi. Lần gọi LLM tốn 2–3 giây mỗi câu, trong khi ngân sách thời gian chặt. Nếu (b) ≈ (c) thì bỏ lần gọi LLM.
- **Đo:** ΔPA; số câu chuyển giữa sai, đúng và abstain; lỗi sai kiểu và sai kỳ nói riêng; thời gian mỗi câu.

### E5: fine-tune bộ sinh mã

- **Là gì:**
  1. tạo dữ liệu bằng rejection sampling trên train (3.3);
  2. fine-tune LoRA trên chính model đang dùng, để tổng tham số không đổi;
  3. huấn luyện có biến thể rulebook;
  4. so sánh model đã fine-tune với model prompting trên **họ định dạng bị giữ lại** (leave-family-out).
- **Vì sao:** đây là hướng duy nhất có thể giảm đồng thời lỗi sinh mã và thời gian mỗi câu. Lợi ích trên ngành mới thì chưa chắc (3.3).
- **Đo:** PA, tỉ lệ lỗi thực thi và thời gian mỗi câu, tách theo họ đã gặp và họ bị giữ lại; độ nhạy khi rulebook thay đổi (có làm theo quy tắc mới không).
- **Điều kiện bắt đầu:** E2 đã ổn định API, vì dữ liệu huấn luyện viết bằng API đó. Cần GPU huấn luyện; T4 của Kaggle chỉ đủ cho QLoRA chậm, nên có thể phải thuê GPU.
- **Mức cần thiết:** khám phá. Chỉ đưa vào bản nộp nếu thắng prompting trên họ bị giữ lại.

**Thứ tự dự kiến:** E0 (+ chạy lặp) → E3 và E2 (cài song song, đo riêng) → E4 → E1 → E5 nếu còn thời gian → một lượt public trên Kaggle để xác nhận trước khi đóng gói Docker.

---

## 6. Phương pháp đánh giá

**Phân loại kết quả mỗi câu:**
- đúng; sai;
- abstain, chia theo nguyên nhân:
  - không chương trình nào chạy ra đáp án;
  - bị kiểm tra từ chối;
  - các mẫu không đồng thuận;
  - sai format.

Cách chia này cho thấy PA tăng vì trả lời đúng hơn hay chỉ vì bỏ qua nhiều hơn.

**Chỉ số:**
- PA;
- Coverage = (đúng + sai) / N;
- Precision = đúng / (đúng + sai);
- thời gian mỗi câu, vì ngân sách là 6 giờ.

**Thống kê:**
- Điểm mỗi câu có độ lệch chuẩn khoảng 0,8–0,9, nên sai số chuẩn của PA khoảng 0,07 trên 120 câu, 0,03 trên 1000 câu và 0,016 trên 3000 câu.
- So sánh theo cặp kèm bootstrap CI 95%.
- Báo cáo độ biến thiên giữa các lần chạy lặp cùng cấu hình.

**Phân tích theo nhóm:** theo kiểu đáp án, họ định dạng, ngành, intent, và câu một kỳ so với nhiều kỳ.

**Tổng quát hoá:**
- Đánh giá leave-family-out trên train: ví dụ few-shot và dữ liệu huấn luyện chỉ lấy từ họ khác.
- Các cửa hàng public thuộc họ mới không có đáp án; chỉ dùng tỉ lệ abstain và kiểm tra hợp lý trên chúng làm tín hiệu định tính.

**Chống rò rỉ:**
- ví dụ few-shot không lấy từ cùng cửa hàng;
- không dùng test để huấn luyện hay làm ví dụ.

Lưu ý: các quy tắc chuẩn hoá và kiểm tra được thiết kế khi nhìn vào đáp án train, nên điểm trên train sẽ lạc quan hơn private.

---

## 7. Rủi ro và hạn chế

| # | rủi ro | giảm thiểu |
|---|---|---|
| R1 | **Kiểm tra ngữ nghĩa khi rulebook đổi.** Nếu private định nghĩa lại một đại lượng (ví dụ doanh thu), kiểm tra ngữ nghĩa sẽ ép các đáp án đúng theo rulebook mới vào abstain. Thiệt hại có chặn (0, không phải −1) nhưng là thật | mỗi hợp đồng gắn với một mệnh đề rulebook; tự tắt khi mệnh đề đó không còn trong rulebook của bộ dữ liệu |
| R2 | Ngành mới không có cấu trúc đơn + dòng hàng (sửa xe, chỉ có hoá đơn) | primitive báo "không khả dụng" thay vì đoán; LLM dùng bảng gốc; precision theo ngành từ E0 làm cơ sở cho ngưỡng abstain |
| R3 | Điểm train lạc quan so với private | đánh giá leave-family-out; báo cáo theo họ định dạng |
| R4 | Thời gian: 6 giờ cho số câu chưa biết | đo thời gian mỗi câu ở E0; bỏ lần gọi LLM ở chuẩn hoá nếu E1 cho thấy không cần; ghi đáp án tăng dần |
| R5 | Tái lập khi ban tổ chức chạy lại trên 5090 (kernel và dtype khác với T4) | greedy, seed cố định; đo độ biến thiên; chạy thử trên 2× 5090 thuê |
| R6 | Fine-tune học thuộc quy tắc train và bỏ qua rulebook mới | biến thể rulebook khi huấn luyện; đánh giá trên họ bị giữ lại; chỉ dùng nếu thắng prompting |

---

## 8. Câu hỏi xin ý kiến mentor

1. Cách đặt câu hỏi nghiên cứu (semantic layer + kiểm tra kết quả, fine-tune là hướng mở rộng) đã hợp lý chưa?
2. **Có nên làm hướng SQL không?** Nhóm đang cân nhắc hai phương án:

   **Phương án 1: không làm SQL.** LLM, có hoặc không fine-tune, chỉ sinh chương trình Python dùng semantic layer.

   | | |
   |---|---|
   | Ưu điểm | dùng lại nguyên pipeline, semantic layer và bộ kiểm tra hiện có; các phép nghiệp vụ (quy đổi đơn vị, kỳ âm lịch, làm tròn half-up, tie-break theo mã) đã là hàm Python; ít biến số, nên dễ tách riêng tác động của fine-tune |
   | Nhược điểm | dữ liệu fine-tune chỉ có thể tự sinh từ 3000 câu train (khoảng 30 cửa hàng, khoảng 9 họ định dạng), nên dễ học thuộc; không tận dụng được các model và corpus Text-to-SQL công khai |

   **Phương án 2: fine-tune theo SQL.** Các bảng canonical được nạp vào một SQL engine nhúng chạy trong tiến trình, ví dụ DuckDB, vốn đọc thẳng DataFrame, chạy offline và không cần server. Các primitive của semantic layer được đưa thành view hoặc macro SQL. Model sinh SQL được fine-tune trên corpus công khai (Spider, BIRD) cộng với dữ liệu tự sinh từ train.

   | | |
   |---|---|
   | Ưu điểm | thêm nhiều dữ liệu huấn luyện; có thể khởi đầu từ các model đã fine-tune cho Text-to-SQL; SQL là ngôn ngữ khai báo, nhỏ hơn pandas, nên có thể ít lỗi thực thi hơn |
   | Nhược điểm | corpus công khai lệch miền xa (tiếng Anh, DB sạch, không có rulebook); phải viết lại tầng thực thi và kiểm tra; một số phép nghiệp vụ viết bằng SQL dài và dễ sai; tổng tham số vẫn phải ≤ 15B nếu cần thêm một model riêng |

   Câu hỏi cụ thể:
   - Theo kinh nghiệm của anh/chị, lợi ích của corpus Text-to-SQL công khai có còn đáng kể khi miền (câu hỏi tiếng Việt nhiễu, định nghĩa nghiệp vụ trong rulebook) khác xa như vậy không?
   - Nếu chỉ đủ thời gian cho một hướng fine-tune, nên chọn phương án nào?
   - Có nên chạy một thí nghiệm nhỏ để quyết định không: cùng model và cùng tập câu thuộc các họ bị giữ lại, so sánh *prompting SQL* với *prompting Python* về tỉ lệ lỗi thực thi và PA, trước khi đầu tư fine-tune?