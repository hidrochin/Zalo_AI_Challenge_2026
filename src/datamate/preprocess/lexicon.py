"""Word-level rewrites: abbreviations, teen-code, Central/Southern dialect and English business words.

Sources, kept apart so none comes from the test splits (organizers' QnA 2026-11-11):
- the entries written here occur in the TRAIN questions;
- assets/lexicon_external.json: entries from public web pages (URL per entry) and from Gemini over train-only seed
  words (preprocess/lexgen.py, raw replies kept), each passed through `accept`. An entry written here wins.
Ambiguous short forms are only rewritten in a context that fixes their meaning ('k' after a number is 'nghìn' and is
left to the amount rules; 'chi' is 'gì' only after 'hàng/món/cái/là'), and words inside a matched shop name are never
touched.
"""

import json
import re
import unicodedata
from pathlib import Path

from datamate.text import fold

EXTERNAL_PATH = Path(__file__).resolve().parents[3] / "assets" / "lexicon_external.json"

# exact lowercase word (diacritics significant) -> replacement; every key occurs in the train questions
WORDS: dict[str, str] = {
    # business abbreviations
    "dthu": "doanh thu", "sp": "sản phẩm", "ck": "chuyển khoản", "tm": "tiền mặt", "hd": "hoá đơn",
    "hnay": "hôm nay", "hqua": "hôm qua",
    # question words and particles
    "bnhiu": "bao nhiêu", "bn": "bao nhiêu", "dc": "được", "đc": "được", "đươc": "được", "duoc": "được",
    "ko": "không", "hok": "không", "hông": "không", "khoong": "không", "j": "gì", "z": "vậy", "dị": "vậy",
    "trc": "trước", "gium": "giùm", "giup": "giúp",
    # English business words
    "revenue": "doanh thu", "total": "tổng", "bill": "đơn", "order": "đơn", "check": "xem", "hot": "bán chạy",
}
# two-word phrases (lowercase) -> replacement
PHRASES: dict[tuple[str, str], str] = {("best", "seller"): "bán chạy nhất", ("bao", "nhiu"): "bao nhiêu"}
# three-word phrases
PHRASES3: dict[tuple[str, str, str], str] = {}
# frequent business phrases typed without diacritics (a 9B rewrite turned 'xem gium' into 'xem giảm' and
# 'cua hang' into 'của hàng'); restored here, outside name spans, so the rewrite starts from the right words
_ASCII_PHRASES = {
    "cua hang": "cửa hàng", "bao nhieu": "bao nhiêu", "hom nay": "hôm nay", "hom qua": "hôm qua", "ton kho": "tồn kho",
    "don hang": "đơn hàng", "khach hang": "khách hàng", "ti le": "tỉ lệ", "phan tram": "phần trăm", "trung binh": "trung bình",
    "binh quan": "bình quân", "gia tri": "giá trị", "mat hang": "mặt hàng", "san pham": "sản phẩm", "nhan vien": "nhân viên",
    "tuan truoc": "tuần trước", "thang truoc": "tháng trước", "thang nay": "tháng này", "tuan nay": "tuần này",
    "nam nay": "năm nay", "nam ngoai": "năm ngoái", "ban duoc": "bán được", "ban ra": "bán ra", "tong cong": "tổng cộng",
    "cao nhat": "cao nhất", "nhieu nhat": "nhiều nhất", "chay nhat": "chạy nhất", "lai gop": "lãi gộp", "cong no": "công nợ",
    "con no": "còn nợ", "tra no": "trả nợ", "bi huy": "bị huỷ", "huy don": "huỷ đơn", "ca sang": "ca sáng",
    "ca chieu": "ca chiều", "gan nhat": "gần nhất", "han dung": "hạn dùng", "het han": "hết hạn", "nhap hang": "nhập hàng",
    "tang hay": "tăng hay", "hay giam": "hay giảm",
}
PHRASES.update({tuple(k.split()): v for k, v in _ASCII_PHRASES.items()})


def train_vocab(train: Path) -> set[str]:
    """Words of the train split (rulebook, READMEs, questions): a variant equal to one of them, or to one of them without
    its diacritics, can be a real word, so an external entry may not rewrite it (the train forms that are variants are
    already entries here)."""
    words: set[str] = set()
    texts = [f.read_text(encoding="utf-8") for f in [train / "rulebook.md", *sorted((train / "data").glob("*/README.md"))]
             if f.exists()]
    qs = train / "questions.jsonl"
    texts += [json.loads(line)["question"] for line in qs.read_text(encoding="utf-8").splitlines() if line.strip()] if qs.exists() else []
    for t in texts:
        words |= set(re.findall(r"[^\W\d_]+", unicodedata.normalize("NFC", t.lower())))
    return words | {fold(w) for w in words}


def accept(variant: str, standard: str, vocab: set[str], standards: set[str] | None = None) -> str | None:
    """Why an external entry is refused, or None. The variant is one to three words of letters, not the standard form
    with or without diacritics, and not standard text itself: a single word may not be a train word (or one letter: those
    need context), and a phrase may not be made of train words only (a synonym would change a business word, and
    normalization keeps the content). standards: the forms a variant may map to (the seeds a generator was given)."""
    toks = variant.split()
    if not 1 <= len(toks) <= 3 or not all(re.fullmatch(r"[^\W\d_]+", t) for t in toks):
        return "shape"
    if standards is not None and standard not in standards:
        return "not a seed"
    if fold(variant) == fold(standard):
        return "same word"
    if len(toks) == 1 and (len(variant) < 2 or variant in vocab):
        return "a word of its own"
    if len(toks) > 1 and all(t in vocab for t in toks):
        return "standard words"
    if standards is not None and variant in standards:
        return "a seed itself"
    return None


def _load_external() -> None:
    if not EXTERNAL_PATH.exists():
        return
    for e in json.loads(EXTERNAL_PATH.read_text(encoding="utf-8")):
        toks = tuple(e["from"].split())
        table = {1: WORDS, 2: PHRASES, 3: PHRASES3}[len(toks)]
        table.setdefault(toks[0] if len(toks) == 1 else toks, e["to"])


_load_external()

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
    if word == "mô":  # Central dialect (mytour.vn, ngonngu.net: mô = đâu); 'chi' = gì, 'ni' = này, 'răng' = sao, 'ri' = thế này
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
