"""Word-level rewrites: abbreviations, teen-code, Central/Southern dialect and English business words.

Each entry was read off the train/public questions (see DATA.md §2.4). Ambiguous short forms are only
rewritten in a context that fixes their meaning ('k' after a number is 'nghìn' and is left to the amount
rules; 'chi' is 'gì' only after 'hàng/món/cái/là'), and words inside a matched shop name are never touched.
"""

# exact lowercase word (diacritics significant) -> replacement
WORDS: dict[str, str] = {
    # business abbreviations
    "dthu": "doanh thu", "dthuu": "doanh thu", "doanhthu": "doanh thu", "dth": "doanh thu",
    "sp": "sản phẩm", "sl": "số lượng", "slg": "số lượng", "nv": "nhân viên", "ncc": "nhà cung cấp",
    "hsd": "hạn sử dụng", "ck": "chuyển khoản", "tm": "tiền mặt", "tb": "trung bình", "tbinh": "trung bình",
    "đh": "đơn hàng", "dh": "đơn hàng", "hđ": "hoá đơn", "hd": "hoá đơn", "đt": "điện thoại", "lnhuan": "lợi nhuận",
    "cty": "công ty", "tg": "thời gian", "kh": "khách hàng", "khg": "không", "ng": "người",
    "hnay": "hôm nay", "hqua": "hôm qua", "hwa": "hôm qua", "hum": "hôm",
    # question words and particles
    "bnhiu": "bao nhiêu", "bnhieu": "bao nhiêu", "bnh": "bao nhiêu", "bn": "bao nhiêu", "baonhieu": "bao nhiêu",
    "dc": "được", "đc": "được", "dk": "được", "đk": "được", "đươc": "được", "đượt": "được",
    "ko": "không", "kô": "không", "hok": "không", "hông": "không", "hổng": "không", "khum": "không", "hem": "không",
    "khoong": "không",
    "j": "gì", "jì": "gì", "z": "vậy", "zậy": "vậy", "zạ": "vậy", "dzạ": "vậy", "dzậy": "vậy", "zị": "vậy",
    "dzị": "vậy", "zay": "vậy", "dị": "vậy", "rứa": "vậy",
    "trc": "trước", "trk": "trước", "trươc": "trước", "trog": "trong", "trg": "trong", "cko": "cho",
    "mik": "mình", "mk": "mình", "mh": "mình", "mềnh": "mình", "mìn": "mình",
    "bít": "biết", "bit": "biết", "mún": "muốn", "mun": "muốn", "nhut": "nhất", "nhứt": "nhất",
    "kủa": "của", "jùm": "giùm", "jum": "giùm", "dùm": "giùm", "wán": "quán", "wa": "qua", "lun": "luôn",
    "ròi": "rồi", "rùi": "rồi", "roài": "rồi", "gồi": "rồi", "mần": "làm", "dề": "về", "nớ": "đó",
    "gium": "giùm", "giup": "giúp", "duoc": "được",
    "chừ": "giờ", "sốp": "shop", "bná": "bán", "đươcj": "được", "zìa": "về", "dìa": "về", "dúp": "giúp",
    # English business words
    "revenue": "doanh thu", "total": "tổng", "bill": "đơn", "bills": "đơn", "order": "đơn", "orders": "đơn",
    "check": "xem", "hot": "bán chạy", "ship": "giao hàng", "shipping": "giao hàng", "customer": "khách hàng",
    "customers": "khách hàng", "product": "sản phẩm", "products": "sản phẩm", "item": "mặt hàng",
    "items": "mặt hàng", "profit": "lợi nhuận", "stock": "tồn kho", "cancel": "huỷ", "refund": "trả hàng",
    "discount": "giảm giá", "cash": "tiền mặt", "staff": "nhân viên", "debt": "nợ", "today": "hôm nay",
    "yesterday": "hôm qua", "and": "và", "per": "mỗi",
}
# two-word phrases (lowercase) -> replacement
PHRASES: dict[tuple[str, str], str] = {
    ("best", "seller"): "bán chạy nhất", ("last", "week"): "tuần trước", ("this", "month"): "tháng này",
    ("last", "month"): "tháng trước", ("this", "week"): "tuần này", ("bao", "nhiu"): "bao nhiêu",
    ("bi", "nhiêu"): "bao nhiêu", ("bi", "nhiu"): "bao nhiêu", ("bây", "chừ"): "bây giờ",
}
# frequent business phrases typed without diacritics (a 9B rewrite turned 'xem gium' into 'xem giảm' and
# 'cua hang' into 'của hàng'); restored here, outside name spans, so the rewrite starts from the right words
_ASCII_PHRASES = {
    "cua hang": "cửa hàng", "bao nhieu": "bao nhiêu", "hom nay": "hôm nay", "hom qua": "hôm qua", "ton kho": "tồn kho",
    "don hang": "đơn hàng", "khach hang": "khách hàng", "doanh so": "doanh số", "ti le": "tỉ lệ", "ty le": "tỷ lệ",
    "phan tram": "phần trăm", "trung binh": "trung bình", "binh quan": "bình quân", "gia tri": "giá trị",
    "mat hang": "mặt hàng", "san pham": "sản phẩm", "nhan vien": "nhân viên", "tuan truoc": "tuần trước",
    "thang truoc": "tháng trước", "thang nay": "tháng này", "tuan nay": "tuần này", "nam nay": "năm nay",
    "nam ngoai": "năm ngoái", "ban duoc": "bán được", "ban ra": "bán ra", "tong cong": "tổng cộng", "cao nhat": "cao nhất",
    "nhieu nhat": "nhiều nhất", "chay nhat": "chạy nhất", "lai gop": "lãi gộp", "loi nhuan": "lợi nhuận",
    "cong no": "công nợ", "con no": "còn nợ", "tra no": "trả nợ", "bi huy": "bị huỷ", "huy don": "huỷ đơn",
    "ca sang": "ca sáng", "ca chieu": "ca chiều", "ca toi": "ca tối", "gan nhat": "gần nhất", "han dung": "hạn dùng",
    "het han": "hết hạn", "nhap hang": "nhập hàng", "tang hay": "tăng hay", "hay giam": "hay giảm",
}
PHRASES.update({tuple(k.split()): v for k, v in _ASCII_PHRASES.items()})

# 'k' / 'v' / 'th' / 'nhiu' / 'mô' / 'chi' / 'ni' / 'răng' / 'nhiêu' / 'vs' need their neighbours
_TH_NEXT = {"trước", "trc", "này", "nay", "rồi", "roi", "sau", "liền", "lien", "vừa", "vua", "qua"}
_NI_PREV = {"tháng", "tuần", "năm", "bữa", "hôm", "ngày", "quán", "tiệm", "hàng", "shop", "đợt", "kỳ", "quý", "lúc",
            "thang", "tuan", "nam", "bua", "hom", "ngay", "quan", "tiem", "hang"}
_CHI_PREV = {"hàng", "món", "cái", "là", "làm", "loại", "phẩm", "thứ", "hang", "mon", "cai", "la", "loai", "thu"}
_CHI_NEXT_KEEP = {"phí", "tiêu", "nhánh", "tiết", "trả", "ra", "phi", "tieu", "nhanh", "tiet", "tra"}
_MORE = {"nhất", "nhat", "nhut", "hơn", "hon", "lắm", "lam"}


def contextual(prev: str | None, word: str, nxt: str | None, nxt2: str | None, is_last: bool) -> str | None:
    """Replacement for a word whose meaning depends on its neighbours (all lowercase), or None."""
    if word == "k" and not (prev or "").isdigit():
        return "không"
    if word == "v" and is_last:
        return "vậy"
    if word == "th" and (nxt in _TH_NEXT or (nxt or "").isdigit()):
        return "tháng"
    if word in ("thang", "ngay", "tuan") and (nxt or "").isdigit():  # 'thang 7' is never 'thắng 7'
        return {"thang": "tháng", "ngay": "ngày", "tuan": "tuần"}[word]
    if word == "nam" and (nxt or "").isdigit() and len(nxt) == 4:
        return "năm"
    if word in ("nhiu", "nhìu"):
        if nxt in _MORE or nxt2 in _MORE:
            return "nhiều"
        return "nhiêu" if prev == "bao" else "bao nhiêu"
    if word == "nhiêu" and prev not in ("bao", "bi"):
        return "bao nhiêu"
    if word == "mô":
        return "đâu" if prev in ("ở", "o") else "nào"
    if word == "chi" and prev in _CHI_PREV and nxt not in _CHI_NEXT_KEEP:
        return "gì"
    if word == "ni" and prev in _NI_PREV:
        return "này"
    if word == "răng" and is_last:
        return "sao"
    if word == "vs":
        return "với" if prev == "so" else "so với"
    if word == "ri" and is_last:
        return "vậy"
    return None
