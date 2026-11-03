# DataMate: Đề xuất v3, sinh mã có nhận thức ngữ nghĩa (Semantic-aware Code Generation)

**Trạng thái:** bản nháp ngày 2026-11-03, dùng để trao đổi với mentor.
**Thay cho:** §2.2–2.6 của [PROPOSAL_V2.md](PROPOSAL_V2.md). Các phần sau giữ nguyên như [PROPOSAL.md](PROPOSAL.md): ingestion (§2.1), Docker (§2.5), tuân thủ (§9) và các ràng buộc (một người làm, chỉ prompting, tổng ≤ 15B tham số, chạy offline).
**Bằng chứng:** [DATA.md](../DATA.md) (phân tích dữ liệu), các lượt chạy trong `outputs/` và hai lần nộp public.
**Câu hỏi nghiên cứu:** *Một semantic layer tất định cải thiện được bao nhiêu về độ chính xác, độ tin cậy khi thực thi và khả năng tái lập của chương trình do LLM sinh ra, trong bài toán hỏi đáp trên dữ liệu có cấu trúc?*

---

## 0. Tóm tắt

- **Bài toán thật sự** gồm ba phần:
  1. hiểu đúng các file lộn xộn;
  2. tính đúng *con số mà ban tổ chức tính theo rulebook*;
  3. quyết định trả lời hay bỏ qua dưới metric có phạt.

  Phần khó nhất không nằm ở ngôn ngữ mà ở **định nghĩa nghiệp vụ**: đơn nào được tính, doanh thu là gì, đơn vị nào, làm tròn ra sao.
- **Kết quả hiện tại:**
  - public PA **0,040**, precision ≈ 52%;
  - dev120 trên train PA **0,375**, precision ≈ 73%.

  Khoảng cách giữa dev và public là vấn đề chính. Với metric +1/0/−1, precision quyết định điểm nhiều hơn coverage.
- **Hướng đề xuất:** giữ LLM sinh mã pandas (kiến trúc A), nhưng gom kiến thức nghiệp vụ vào một **semantic layer có kiểm thử**: primitive, kiểu, đơn vị, bất biến, version. Thêm **tiền xử lý câu hỏi** và **kiểm tra kết quả** tách thành hai tầng, cấu trúc và ngữ nghĩa.
- **Không làm:**
  - predefined intent tools, vì không biết phân phối của tập private;
  - hybrid agent, vì tốn kém và khó tái lập;
  - Text-to-SQL chỉ thử khi lỗi thực thi còn cao (E4, có điều kiện).
- **Thí nghiệm:**
  - E0: baseline trên toàn bộ train, chạy trên Kaggle, **đang chạy**;
  - E1: thêm tiền xử lý;
  - E2: mở rộng semantic layer;
  - E3: kiểm tra kết quả;
  - E4: DuckDB, có điều kiện.

  E2 và E3 được cài song song nhưng **đánh giá tách riêng**.

---

## 1. Bài toán và thước đo

| khía cạnh | nội dung |
|---|---|
| Đầu vào | `{id, business_id, question, as_of_date}`, thư mục dữ liệu của cửa hàng (CSV / XLSX / SQLite và README), cộng `rulebook.md` và `calendar.csv` của split |
| Đầu ra | một dòng JSON có kiểu: number, percent, string, date, boolean, list, table, unanswerable hoặc abstain |
| So khớp | kiểu phải đúng, giá trị khớp chính xác (percent làm tròn 2 chữ số, chuỗi chuẩn hoá nhưng phải đúng dấu) |
| Metric | **Penalized Accuracy**: +1 đúng, 0 abstain, −1 sai. Chỉ nên trả lời khi P(đúng) > 0,5 |
| Ràng buộc | model mở trọng số, tổng ≤ 15B; offline; 2× RTX 5090; ≤ 6 giờ cho toàn bộ private; chạy lại phải lệch \|ΔPA\| ≤ 0,03 |
| Private test | có **loại hình kinh doanh chưa thấy** và rulebook riêng |

**Hệ quả của metric.** Mỗi câu được trả lời có giá trị kỳ vọng 2p − 1:
- precision 80%: mỗi câu trả lời đáng +0,6;
- precision 52% (public hiện tại): chỉ đáng +0,04.

Vì vậy mọi thay đổi phải báo cáo cả số câu **sai**, không chỉ PA.

---

## 2. Phân tích dữ liệu và hệ quả cho thiết kế

Mỗi mục có ba phần: điều quan sát được, con số đo, và lựa chọn thiết kế hoặc thí nghiệm mà nó dẫn tới.

### 2.1 File dữ liệu (CSV / XLSX / SQLite)

**Quan sát**

| đặc điểm | số liệu (train) |
|---|---|
| Số cửa hàng, số file | 30 cửa hàng; 107 xlsx, 20 csv, 1 sqlite |
| Số bảng mỗi cửa hàng | từ 1 (sổ bán hàng phẳng) đến 17 (`pos.sqlite`) |
| Kích thước | 6 nghìn – 306 nghìn dòng sau làm sạch; mỗi cửa hàng có 12–20 tháng dữ liệu |
| "Họ" định dạng lặp lại | khoảng 9 họ: KiotViet-like, Sapo-like, "Sổ chi tiết bán hàng", sổ tay 2 dòng tiêu đề, CSV phẳng, CSV quan hệ, chỉ có hoá đơn, SQLite POS, nguồn hỗn hợp |
| Public so với train | 9/14 cửa hàng thuộc họ đã gặp; 5 cửa hàng mới (điện thoại, sửa xe, mẹ và bé, "3 danh sách") |

Dữ liệu bị làm bẩn có chủ ý:
- tên cột bị đổi, viết tắt, trộn Anh–Việt (`Thanh tien`, `T.Tiền`, `Slg`);
- có dòng tiêu đề, dòng lọc và dòng trống; tiêu đề gộp 2 dòng; mỗi tháng một sheet;
- ngày trộn nhiều định dạng trong cùng một cột (`01/02/2025`, số serial Excel, `1 thg 1 2025`);
- tiền viết `55k` hay `1,6256tr`; số lượng kèm đơn vị (`3 chai`);
- trạng thái đơn chỉ nằm trong ghi chú; dòng tiếp theo của một đơn bỏ trống nghĩa là giống dòng đầu;
- dòng trùng nằm trong cùng một đơn.

Khả năng của dữ liệu cũng khác nhau rất nhiều giữa các cửa hàng. Tồn kho, nhập hàng, lô, khách, công nợ, nhân viên và quy đổi đơn vị mỗi thứ chỉ có ở một số cửa hàng; biz_113 không có thứ nào, biz_124 có tất cả. Có cửa hàng **không có dòng hàng** (biz_110, dev_009), và dev_012 là phiếu sửa xe ghi việc làm bằng văn bản tự do.

Ngoài ra, file chứa cả **bản ghi sau `as_of_date`**. 75–79% câu có as-of bằng ngày cuối của dữ liệu, nên lỗi quên cắt theo as-of bị che trên tập dev nhỏ.

**Hệ quả**
1. **Ingestion và canonical schema phải chạy tất định, trước khi có LLM.** Đọc bằng calamine, dò dòng tiêu đề, gán vai trò cột từ từ vựng README, forward-fill, khử trùng lặp, chuẩn hoá trạng thái, quy đổi đơn vị gốc, rồi cache một lần cho mỗi cửa hàng. Phần này đã có, ở `INGEST_VERSION` 11.
2. **Chọn pandas, không chọn SQL làm mặc định.** Dữ liệu ≤ 306 nghìn dòng vừa trong bộ nhớ. Phần khó là làm sạch và định nghĩa, và SQL không giải quyết được cả hai. Sau ingestion, đa số câu chỉ cần 1–2 bảng chuẩn (`sales_lines`, `orders`). DuckDB chỉ đáng thử nếu lỗi thực thi pandas còn cao (E4).
3. **Câu hỏi có trả lời được hay không phụ thuộc vào schema và khoảng thời gian, không vào câu chữ.** Cùng một mẫu câu có thể trả lời được ở cửa hàng này mà không ở cửa hàng kia. Vì vậy quyết định unanswerable phải dựa trên khả năng của dữ liệu (capability flags) và việc kỳ hỏi có giao với dữ liệu hay không.
4. **Đơn vị phân tích của tổng quát hoá là họ định dạng và ngành hàng.** Đánh giá chỉ trên các cửa hàng train dễ cho kết quả lạc quan, và đây là lý do giải thích một phần khoảng cách giữa dev và public.

### 2.2 Rulebook

**Quan sát**
- Rulebook khác nhau giữa các split: train 18,5 nghìn ký tự, public 23,8 nghìn.
- Public thêm các ngành mẹ và bé, sửa xe, điện thoại, cùng định nghĩa cho các câu "nhất". Public cũng bỏ một số quy tắc có trong train.
- Quy tắc rất cụ thể và trái trực giác. Ví dụ: doanh thu loại đơn có *bất kỳ* dòng bị trả; doanh thu không trừ voucher cấp đơn; giá trị trung bình một đơn làm tròn half-up đến đồng; tuần tính từ thứ Hai đến Chủ nhật; Tết là 23 tháng Chạp đến mùng 10 tháng Giêng.

**Hệ quả**
1. **Các mặc định dùng chung** (đơn được tính, doanh thu, đơn vị gốc, làm tròn, as-of, khách lẻ) được cài **một lần** trong semantic layer và kiểm thử với gold của train. LLM không phải suy luận lại chúng ở mỗi câu.
2. **Không được giả định rulebook private giống train.** Rulebook vẫn nằm trong prompt và LLM đọc nó lúc inference. Mỗi primitive ghi rõ nó dựa trên định nghĩa nào, kèm `SEMANTIC_VERSION`. Một primitive mà định nghĩa của nó không còn trong rulebook của split phải bị tắt (xem rủi ro R1).
3. Đây là lý do **không** xây intent tool theo câu hỏi (`cancel_rate()`, `top_products()`, ...). Chúng mã hoá cả cách diễn giải câu hỏi lẫn quy tắc của train, nên gãy khi intent hoặc quy tắc mới xuất hiện. Primitive cấp thấp hơn (đơn, dòng, doanh thu, kỳ) có thể ghép lại cho các intent mới.

### 2.3 Câu hỏi

**Quan sát** (train có 3000 câu, trừ khi ghi khác)

| hiện tượng | số liệu | ví dụ |
|---|---|---|
| Không dấu | 25,8% train, 26,2% public | `doanh thu thang 3 bao nhieu` |
| Viết tắt, teen-code | `dthu` 646 câu, `bnhiu` 396, `bn` 222, `z` 155, `dc/đc` 274 | `dthu th7/26 bnhiu z` |
| Dư phím Telex (gõ sai) | khoảng 100 câu | `nhaats`, `cuwar`, `ddeens` |
| Phương ngữ (public) | `ni`, `rứa`, `mô`, `chừ`, `dề` | `tháng ni bán răng rồi` |
| Trộn tiếng Anh | khoảng 7,6% (regex thô) | `revenue`, `bill`, `top`, `hot` |
| Ngày và tiền nén | 781 câu có ngày được chuẩn hoá | `th7/26`, `3/25`, `6-2025`, `quý 2/2025`, `3tr7`, `152k` |
| Nhắc tên trong danh mục | 678 câu có tên khớp chính xác danh mục | `Thuoc la Vinataba` → `Thuốc lá Vinataba` |
| Nhiều kỳ trong một câu | 389 câu (13%) có ≥ 2 kỳ thời gian | "tháng 3 so với tháng 2", "từ th1 tới th3 … tháng sau cao hơn tháng trước" |
| Câu ghép | khoảng 10% có từ nối ("kèm", "lần lượt", "so với", "cùng kỳ") | "liệt kê 5 khách nợ nhiều nhất, kèm số tiền" |
| Trùng bề mặt public với train | chỉ 4,2% câu public khớp nguyên văn một khung câu train; Jaccard trung vị 0,46 | |
| Phân phối intent lệch | public có câu về khách gấp khoảng 2 lần, câu về lợi nhuận gấp khoảng 7 lần, câu về huỷ và tồn ít hơn | |

Cùng một intent có thể đòi kiểu đáp án khác nhau: "bn đơn bị huỷ" cần một số đếm, "tỉ lệ huỷ" cần phần trăm. Sai kiểu là sai cả câu.

**Hệ quả**
1. **Tiền xử lý bằng thuật toán trước, sau đó đúng một lần gọi LLM.** Phần quy tắc:
   - bộ giải Telex có kiểm tra âm tiết hợp lệ (âm đầu + vần), dựa trên 118 vần lấy từ train;
   - từ điển viết tắt, teen-code và phương ngữ, kèm quy tắc theo ngữ cảnh (`k` sau một số là nghìn);
   - chuẩn hoá tiền và ngày;
   - nhận diện tên trong danh mục cửa hàng, có tôn trọng dấu (`tháng` không phải `Thắng`).

   Lần gọi LLM trả về JSON `{normalized, answer_type, top_k, sub_questions, entities}`. Một lần viết lại sẽ bị **loại** nếu làm đổi một con số, mất một tên chính xác, hoặc bỏ một từ thuộc tên trong danh mục.
2. **Giải thời gian bằng quy tắc** (`hints.py`): tạo lời gọi kỳ (`month(2025, 3)`, `quarter(2025, 2)`, `prev_period(...)`, `ytd()`), đã khớp gold ở 164/164 câu "% so với kỳ trước". Bộ giải thời gian dùng bản đã qua quy tắc. Solver thấy **cả câu gốc lẫn câu đã chuẩn hoá**, và câu gốc thắng nếu hai bản mâu thuẫn.
3. **Truy xuất ví dụ theo khung câu đã chuẩn hoá** thay vì câu thô, vì độ trùng bề mặt thấp.
4. **Thí nghiệm E1** đo xem tiền xử lý có thật sự giúp không. Phân tích lỗi trước đây cho thấy lỗi hiểu sai câu là **thứ yếu** so với lỗi định nghĩa, nên kỳ vọng mức cải thiện vừa phải.

### 2.4 Câu trả lời

**Quan sát** (gold train)

| number | percent | list | date | string | unanswerable | boolean | table |
|---|---|---|---|---|---|---|---|
| 1884 (62,8%) | 578 (19,3%) | 193 | 145 | 88 | 70 (2,3%) | 23 | 19 |

- **82% là number hoặc percent**, và phải khớp chính xác: lệch 1 đồng hay dùng một định nghĩa doanh thu khác đều tính −1.
- Mọi đáp án number đều là số nguyên. 49% có giá trị ≥ 10⁶ (tiền VND). **Chỉ 2/1884 bằng 0**, nên một đáp án 0 gần như chắc chắn là lỗi lọc hoặc join.
- Percent: 23,5% âm (thay đổi so với kỳ trước); chỉ 8 câu có |x| > 100.
- List chỉ dài 2, 3 hoặc 5. Date có dạng `YYYY-MM-DD` (129 câu) hoặc `YYYY-MM` (16 câu, câu hỏi "tháng nào").
- Unanswerable có 70 câu, nguyên nhân: thiếu trường dữ liệu (khoảng 45), kỳ nằm ngoài dữ liệu (khoảng 16), câu hỏi chủ quan (khoảng 8), cần kiến thức ngoài (1). Câu có kỳ giao một phần với dữ liệu **vẫn trả lời được**.

Ngoài ra có những gợi ý trong câu hỏi gần như quyết định hoàn toàn kiểu đáp án, đo trên gold train:
- "tháng nào" → `YYYY-MM`;
- "%" hoặc "tỉ lệ" → percent;
- "top N" → list có tối đa N phần tử.

Các gợi ý này đúng 100% trên 808 câu. Ngoài ra, đáp án dạng ngày luôn nằm trong kỳ được hỏi (141/141), và đáp án top-k luôn có tối đa k phần tử (120/120).

**Hệ quả**
1. **Hậu xử lý bảo đảm đúng format** (`finalize`):
   - số nguyên khi giá trị là nguyên; percent làm tròn 2 chữ số, chuẩn hoá −0 thành 0;
   - ngày đúng regex; bảng lưu theo cột thì chuyển vị;
   - bất cứ thứ gì sai dạng → abstain.

   Không bao giờ để một lỗi format biến thành −1.
2. **Kỳ vọng về đáp án từ câu hỏi** (kiểu, tháng hay ngày, k):
   - gợi ý đạt 100% trên gold là kiểm tra **cứng**: được 1 vòng kiểm lại, sau đó abstain;
   - suy đoán của LLM chỉ là kiểm tra **mềm**.
3. **Kiểm tra kết quả** (mục 4.5), gồm bất biến cấu trúc và tính lại ngữ nghĩa, nhắm vào loại lỗi tệ nhất dưới PA: con số *trông hợp lý nhưng sai*.
4. **Unanswerable cần bằng chứng tất định** (thiếu trường, kỳ ngoài dữ liệu). Khi không chắc thì abstain, không tuyên bố unanswerable.

### 2.5 Kết quả hiện tại và phân tích lỗi

| lượt chạy | PA | đúng / sai / abstain | precision |
|---|---|---|---|
| dev120 v1 (baseline) | 0,100 | 57 / 45 / 18 | 56% |
| dev120 v2 (`sales_lines`, sửa ingestion) | 0,208 | 56 / 31 / 33 | 64% |
| dev120 v4 (gợi ý thời gian, `orders()`, kiểm tra đáp án) | **0,375** | 72 / 27 / 21 | 73% |
| public lần 1 (kernel v5) | −0,110 | khoảng 217 / 327 / 456 | khoảng 40% |
| public lần 2 (kernel v6) | **0,040** | khoảng 415 / 375 / 210 | khoảng 52% |

Lần nộp public đầu tiên còn chịu lỗi hạ tầng: server LLM dừng giữa chừng, 340 câu bị abstain. Lần thứ hai không còn lỗi đó.

Phân loại 27 câu sai của dev120 v4 và triage các câu sai v1–v3 cho thấy ba nhóm lỗi:

| nhóm lỗi | ví dụ | cách xử lý đề xuất |
|---|---|---|
| **Logic nghiệp vụ**: định nghĩa sai, chiếm đa số | trung bình một đơn lệch ×2 (chia cho số dòng thay vì số đơn); "đơn ≥ X" ra 22381 trong khi gold là 1215 (đếm dòng); tỉ lệ huỷ 44,94% trong khi gold là 3,16% (map trạng thái sai); lợi nhuận gộp ×3; khách lẻ lọt vào bảng xếp hạng | semantic layer: primitive có định nghĩa rõ, kiểm thử với gold, cùng kiểm tra ngữ nghĩa (shadow) |
| **Lỗi sinh mã**: KeyError, TypeError, lọc rỗng | quên cột `counted`, quên `in_period`, sai tên cột; lọc rỗng → 0 hoặc "không có dữ liệu" | canonical schema ổn định, ép kiểu khi ingest, API helper nhỏ và ổn định, 1 vòng sửa lỗi |
| **Suy luận hoặc hiểu câu** | đọc `quý 2/2025` thành tháng 2; nhầm `5/25` là ngày 25/5; trả sai kiểu (percent thay vì số đếm) | chuẩn hoá tham số (tiền xử lý, gợi ý thời gian), kiểm tra kiểu đáp án, abstention |

Đáng chú ý là **44,94% vẫn là một đáp án hợp lệ về cấu trúc**: kiểm tra cấu trúc không bắt được lỗi định nghĩa, chỉ kiểm tra ngữ nghĩa mới bắt được.

---

## 3. Phản biện các kiến trúc thay thế

Bốn phương án ban đầu được đối chiếu với các quan sát ở mục 2.

| phương án | đánh giá | quyết định |
|---|---|---|
| **A. LLM + pandas sandbox** | tổng quát nhất và đã có sẵn. Điểm yếu là logic nghiệp vụ nằm trong mã sinh ra, nên khó kiểm soát và khó debug | **giữ làm khung**; bù điểm yếu bằng semantic layer (E2) và kiểm tra kết quả (E3) |
| **B. LLM + predefined tools theo intent** | precision cao trên các intent đã biết. Nhưng private có ngành và quy tắc mới, chỉ 4,2% khung câu public trùng train, và new intent (bảo hành, hoa hồng, thu cũ đổi mới) đã xuất hiện ngay ở public. Một tool áp quy tắc cũ sẽ **sai một cách tự tin** | **bỏ**; thay bằng *primitive* cấp thấp mà LLM ghép lại |
| **C. LLM + Text-to-SQL (DuckDB)** | SQL không giải quyết ingestion và định nghĩa, vốn là hai nguồn lỗi chính. Dữ liệu nhỏ, pandas đủ. Lợi thế duy nhất có thể có: model sinh SQL ít lỗi thực thi hơn pandas | **có điều kiện (E4)**: chỉ thử nếu lỗi thực thi > khoảng 10% sau E2, và giữ nguyên mọi yếu tố khác |
| **D. Hybrid agent** (SQL, tools, sandbox, điều phối nhiều bước) | nhiều lời gọi LLM cho mỗi câu; khó tái lập khi yêu cầu \|ΔPA\| ≤ 0,03; ngân sách khoảng 7 giây mỗi câu; model 9B điều phối kém | **bỏ** |

Hướng còn lại là **A cộng một semantic layer có kiểm thử**: LLM vẫn linh hoạt khi gặp intent mới, còn phần định nghĩa được kiểm soát tập trung.

---

## 4. Pipeline đề xuất

```
 Raw files (CSV · XLSX · SQLite · README)
        │
        ▼
 ┌─────────────────────────────┐
 │ 1. Ingestion & Preprocessing│  tất định, cache mỗi cửa hàng; INGEST_VERSION
 └──────────────┬──────────────┘
                ▼
 ┌─────────────────────────────┐
 │ 2. Canonical Schema         │  sales_lines(), orders(), products, customers, …
 └──────────────┬──────────────┘  vai trò cột, đơn vị gốc, trạng thái chuẩn
                ▼
 ┌─────────────────────────────┐
 │ 3. Semantic Layer           │  định nghĩa · primitive · kiểu/đơn vị · bất biến
 │    (dm.py, SEMANTIC_VERSION)│  kiểm thử hợp đồng với gold train
 └──────────────┬──────────────┘
                │        Câu hỏi ──► Tiền xử lý câu hỏi (quy tắc + 1 lần LLM)
                │                    └► gợi ý thời gian, tên danh mục, kỳ vọng đáp án
                ▼                          │
 ┌─────────────────────────────┐ ◄─────────┘
 │ 4. Prompt + LLM sinh mã     │  rulebook, README, schema card, ví dụ train,
 │    (Qwen3.5-9B, greedy)     │  câu gốc + câu chuẩn hoá
 └──────────────┬──────────────┘
                ▼
 ┌─────────────────────────────┐   lỗi thực thi → 1 vòng sửa
 │ 5. Sandbox                  │   (worker giữ bảng trong bộ nhớ, timeout, giới hạn RAM)
 └──────────────┬──────────────┘
                ▼
 ┌─────────────────────────────┐   cứng: mâu thuẫn → 1 vòng kiểm lại → abstain
 │ 6. Result Validation        │   cấu trúc: kiểu, kỳ, k, bất biến
 │                             │   ngữ nghĩa: tính lại bằng primitive (shadow)
 └──────────────┬──────────────┘
                ▼
 ┌─────────────────────────────┐
 │ 7. Finalize + Abstention    │──► answer │ unanswerable │ abstain
 └─────────────────────────────┘    submission.jsonl + logs/<id>.json
```

### 4.1 Ingestion và canonical schema (đã có)
Bước này biến mỗi cửa hàng thành các bảng sạch, kèm một schema card cho prompt. Từ đó dựng `sales_lines()` (dòng hàng đã nối với header đơn, đơn vị gốc, cờ `counted`) và `orders()` (một dòng mỗi đơn).

Các kiểm thử hồi quy so với gold train hiện đạt:
- doanh thu tháng 168 câu;
- số đơn huỷ và tỉ lệ huỷ 204/208;
- top-k 32/33.

### 4.2 Semantic layer, một hợp đồng ngữ nghĩa có kiểm thử
Mỗi primitive phải thoả bốn điều kiện:
1. định nghĩa nghiệp vụ được ghi trong docstring và trích từ rulebook;
2. quy tắc tập trung ở một chỗ (đơn được tính, đơn vị gốc, as-of, làm tròn);
3. kiểu và đơn vị rõ ràng (tiền là số nguyên VND, percent đã nhân 100, số đếm là số nguyên);
4. có **kiểm thử hợp đồng** với gold train, và mọi thay đổi định nghĩa phải tăng `SEMANTIC_VERSION`.

Lệnh `datamate semantic-check` chạy các kiểm thử này trong 18 giây, không cần LLM. Kết quả ở `SEMANTIC_VERSION` 2026-11-03.1:

| hợp đồng | câu khớp mẫu | tính được | khớp gold | bỏ qua |
|---|---|---|---|---|
| `avg_order_value(p)` (mới) | 167 | 167 | **167/167** | 0 |
| `revenue(p)` | 291 | 259 | **259/259** | 14 cửa hàng không có `sales_lines`, 18 câu có nhiều kỳ |
| số đơn (`orders()` + `counted`) | 167 | 136 | **136/136** | 11 không có `sales_lines`, 15 nhiều kỳ |
| số đơn huỷ | 100 | 100 | **100/100** | 0 |
| tỉ lệ huỷ | 104 | 104 | **104/104** | 0 |

Ứng viên tiếp theo cho E2, ưu tiên theo tần suất câu hỏi và lỗi đã gặp:
- `customer_sales(p)`: loại khách lẻ, định danh theo mã, gộp các biến thể tên;
- `qty_sold(p)`: theo đơn vị gốc;
- `debt_at(d)` và `debt_aging`;
- `gross_profit(p)`: lấy giá vốn ghi trên dòng bán;
- `stock_at(d)`.

Primitive **không** phải intent tool. Nó trả về một đại lượng hoặc một bảng, còn LLM vẫn tự lọc, nhóm, xếp hạng và định dạng.

### 4.3 Tiền xử lý câu hỏi (đã có, chưa đo trên dev)
Chi tiết ở mục 2.3. Các quy tắc làm thay đổi 1873/3000 câu train trong khoảng 0,1 giây.

Lần gọi LLM viết lại tốn khoảng 2–3 giây mỗi câu trên Mac. Ở 44 câu mẫu, nó đoán đúng kiểu đáp án 43/44; câu sai có gold là unanswerable. Một lần viết lại không qua kiểm tra sẽ bị bỏ, và pipeline dùng bản đã qua quy tắc.

### 4.4 Sinh mã và sandbox (đã có)
Mô hình là Qwen3.5-9B, chạy greedy, tắt thinking. LLM viết một chương trình pandas ngắn và kết thúc bằng `answer_*()` hoặc `unanswerable()`. Chương trình chạy trong worker process có timeout và giới hạn bộ nhớ; nếu lỗi, có 1 vòng sửa kèm traceback.

### 4.5 Kiểm tra kết quả (mới): cấu trúc và ngữ nghĩa
| tầng | kiểm tra gì | đo offline (log các lượt chạy trước + gold) |
|---|---|---|
| Kỳ vọng từ câu hỏi (cứng) | kiểu đáp án, tháng hay ngày, đáp án ngày nằm trong kỳ hỏi, độ dài top-k | gợi ý đúng 100% trên 808 câu; kỳ 141/141; top-k 120/120 trên gold |
| Bất biến cấu trúc (không cần dữ liệu) | tỉ lệ trong [0, 100]; số đếm nguyên; số ngày ≤ độ dài kỳ; dấu âm chỉ khi câu hỏi về chênh lệch hoặc lãi lỗ; tiền < 1000 đ là sai đơn vị; khách lẻ trong xếp hạng | **0/2930** báo nhầm trên gold; bắt được **4/113** câu sai |
| Ngữ nghĩa (shadow) | nếu câu hỏi đúng một đại lượng có hợp đồng và đúng một kỳ, tính lại bằng primitive; nếu lệch thì gợi ý lời gọi đúng cho vòng kiểm lại | bắt **16/16** câu sai trong phạm vi; **0/44** báo nhầm trên câu đúng |

Số liệu này xác nhận điều đã nêu ở mục 2.5: kiểm tra cấu trúc rất an toàn nhưng chỉ bắt được ít lỗi, còn kiểm tra ngữ nghĩa bắt gần hết lỗi định nghĩa nhưng chỉ áp dụng cho các đại lượng có hợp đồng.

### 4.6 Finalize và abstention
`finalize` bảo đảm format như mục 2.4. Kiểm tra cứng không qua sau vòng kiểm lại dẫn tới abstain. Unanswerable chỉ được đưa ra khi có bằng chứng tất định, nếu không thì abstain.

Câu hỏi mở cho Phase 5: hiệu chỉnh ngưỡng abstain theo precision của từng nhóm intent, dùng chính E0 (mục 5).

---

## 5. Kế hoạch thí nghiệm

Nguyên tắc chung:
- mỗi thí nghiệm **chỉ đổi một yếu tố**; giữ cố định model, rulebook, bộ câu hỏi, cấu hình sinh và môi trường thực thi;
- so sánh **theo cặp** trên cùng các câu, bằng `datamate compare`, có bootstrap CI 95% cho ΔPA.

| # | giả thuyết | nhánh so sánh | giữ cố định | thước đo chính | tiêu chí giữ lại |
|---|---|---|---|---|---|
| **E0** | (baseline) đo pipeline hiện tại trên toàn bộ train để phân tích lỗi theo nhóm | commit `33d0413` (pipeline `98d4c7b`), `--normalize llm`, toàn bộ 3000 câu train, Kaggle 2× T4 | n/a | outcomes, PA theo kiểu / cửa hàng / họ / intent | n/a: là mốc cho E1–E4 và dữ liệu để hiệu chỉnh abstention |
| **E1** | chuẩn hoá câu hỏi giảm lỗi hiểu câu và lỗi kiểu | `--normalize off` vs `llm` | toàn bộ phần còn lại | ΔPA, số sai, chuyển trạng thái đúng↔sai | ΔPA > 0 và số sai không tăng; nếu `llm` không hơn `rules` thì bỏ lần gọi LLM để tiết kiệm thời gian |
| **E2** | primitive có kiểm thử giảm lỗi logic nghiệp vụ | semantic layer hiện tại vs mở rộng (mục 4.2) | **tắt kiểm tra ngữ nghĩa** để không lẫn với E3 | (1) contract test trên gold, không cần LLM; (2) ΔPA và số sai ở các nhóm intent có primitive mới | contract test đạt 100% trên các câu tính được; ΔPA > 0 ở nhóm liên quan |
| **E3** | kiểm tra kết quả chuyển câu sai thành abstain mà không mất câu đúng | kiểm tra tắt / chỉ cấu trúc / cấu trúc + ngữ nghĩa | semantic layer cố định | số sai bị bắt (recall), số đúng bị abstain nhầm, ΔPA | báo nhầm ≤ 1% câu đúng; ΔPA > 0 |
| **E4** (có điều kiện) | sinh SQL (DuckDB) ít lỗi thực thi hơn pandas | pandas vs DuckDB SQL trên **cùng** canonical schema và semantic layer (primitive được expose thành view hoặc macro) | semantic layer, rulebook, câu hỏi, model, môi trường thực thi | tỉ lệ `exec_failure`, ΔPA | chỉ chạy nếu `exec_failure` > khoảng 10% câu sau E2 |

Thứ tự: E0 → E1 → (E2 ∥ E3, cài song song, đo riêng) → E4 nếu cần → một lượt public trên Kaggle để xác nhận.

**E0 đang chạy.** Toàn bộ 3000 câu train được chạy trên Kaggle với đúng pipeline hiện tại. Kết quả nằm ở `outputs/kaggle/result_train/out/train/`, gồm `report.json` và `logs/<id>.json`. Dùng nó để:
1. phân loại lỗi theo ba nhóm ở mục 2.5, theo kiểu đáp án, theo họ định dạng và theo intent;
2. ước lượng precision theo nhóm, làm cơ sở cho chính sách abstain (Phase 5);
3. chọn primitive cho E2 theo số câu sai mà mỗi primitive có thể sửa.

**Việc cần làm thêm cho E2 và E3:** thêm cờ CLI để bật hoặc tắt từng tầng kiểm tra (`--checks off|structural|all`). Hiện cả hai tầng luôn bật.

---

## 6. Phương pháp đánh giá

**Phân loại kết quả mỗi câu:**
- correct, incorrect;
- abstain chia theo nguyên nhân:
  - `exec_failure`: không chương trình nào chạy ra đáp án;
  - `validation`: một kiểm tra đã từ chối đáp án;
  - `vote`: các mẫu không đồng thuận;
  - `format`: finalize từ chối.

Cách chia này giúp thấy rõ PA tăng vì trả lời đúng hơn hay chỉ vì abstain nhiều hơn.

**Chỉ số:**
- PA = (đúng − sai) / N;
- Coverage = (đúng + sai) / N;
- Accuracy_answered = đúng / (đúng + sai), tức precision;
- thời gian mỗi câu.

**Độ tin cậy thống kê:**
- Độ lệch chuẩn của điểm mỗi câu khoảng 0,8–0,9, nên SE của PA khoảng **0,07 trên dev120**, 0,03 trên 1000 câu và 0,016 trên 3000 câu.
- Ví dụ: dev120 v2 → v4 cải thiện +0,167 nhưng CI 95% là [−0,008; 0,358], tức **chưa có ý nghĩa thống kê**.
- Hệ quả: dev120 chỉ dùng để kiểm tra nhanh; quyết định giữ hay bỏ dựa trên toàn bộ train (hoặc một tập ≥ 400 câu phân tầng) chạy trên Kaggle.

**Độ biến thiên và tái lập:** chạy lại **cùng một cấu hình** hai lần (vLLM greedy có thể không tất định khi batch khác nhau) và báo cáo |ΔPA| giữa hai lần. Con số này vừa là sàn nhiễu cho các thí nghiệm, vừa liên quan trực tiếp tới quy định |ΔPA| ≤ 0,03.

**Phân tích theo nhóm:** theo kiểu đáp án, họ định dạng, ngành, intent, và câu một kỳ so với nhiều kỳ.

**Tổng quát hoá:**
- Train chỉ có các họ đã thấy, nên cần đánh giá leave-family-out: các họ được nhận diện từ cấu trúc bảng, và ví dụ few-shot chỉ lấy từ họ khác.
- Các cửa hàng public thuộc họ mới (dev_007/008, dev_011–014) không có gold; chỉ dùng tỉ lệ abstain và kiểm tra hợp lý trên chúng làm tín hiệu định tính.

**Chống rò rỉ:**
- ví dụ few-shot loại câu của cùng cửa hàng;
- không dùng dữ liệu test để huấn luyện hay làm ví dụ;
- không có logic gắn với ID.

Lưu ý: các quy tắc tiền xử lý và kiểm tra được *thiết kế* nhìn vào gold train, nên điểm trên train sẽ lạc quan hơn private.

---

## 7. Rủi ro và hạn chế

| # | rủi ro | giảm thiểu |
|---|---|---|
| R1 | **Kiểm tra ngữ nghĩa và rulebook mới.** Nếu private đổi định nghĩa (ví dụ doanh thu), shadow check sẽ ép các đáp án đúng-theo-rulebook-mới vào abstain. Thiệt hại có chặn (0 điểm, không phải −1) nhưng là thật | mỗi hợp đồng gắn với một mệnh đề trong rulebook; tự tắt hợp đồng khi mệnh đề đó không còn trong rulebook của split |
| R2 | Ngành mới không có cấu trúc đơn + dòng (sửa xe, chỉ có hoá đơn) | primitive phải báo `Unanswerable` hoặc không khả dụng thay vì đoán; LLM dùng bảng thô; precision theo ngành lấy từ E0 |
| R3 | Train lạc quan so với private (mục 6) | báo cáo theo họ định dạng; tập public họ mới làm tín hiệu định tính |
| R4 | Thời gian: 6 giờ, N chưa biết; lần gọi viết lại thêm khoảng 15–20% thời gian | đo s/câu ở E0; nếu E1 cho thấy `rules` ≈ `llm` thì bỏ lần gọi LLM; ghi đáp án tăng dần |
| R5 | Tái lập trên 5090 so với T4 (kernel và dtype khác) | greedy, seed cố định; đo ΔPA giữa các lần chạy lặp; chạy thử khô trên 2× 5090 thuê |
| R6 | Model 9B áp dụng kém các quy tắc chưa từng thấy | rulebook luôn ở trong prompt; abstain khi kiểm tra thất bại; không mã hoá cứng quy tắc mới |

---

## 8. Câu hỏi xin ý kiến mentor

1. Câu hỏi nghiên cứu và phạm vi (A + semantic layer; bỏ B và D; C có điều kiện) đã hợp lý chưa?
2. Có nên đầu tư vào đánh giá **leave-family-out** trên train, hay ưu tiên chạy thêm public để bám sát private hơn?
3. Ngưỡng chấp nhận báo nhầm cho kiểm tra kết quả (≤ 1% câu đúng) có quá chặt hoặc quá lỏng không, khi xét đến rủi ro R1?
4. Với ngân sách GPU Kaggle (khoảng 30 giờ/tuần, mỗi lượt toàn bộ train mất khoảng 4–5 giờ), nên dành bao nhiêu lượt cho thí nghiệm và bao nhiêu lượt để đo độ biến thiên?
