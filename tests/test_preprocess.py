import pandas as pd
import pytest

from datamate.preprocess import EntityIndex, clean, rules_normalize
from datamate.preprocess.amounts import normalize_amounts, normalize_dates
from datamate.preprocess.rewrite import parse
from datamate.preprocess.telex import decode


@pytest.mark.parametrize("tok,word", [
    ("soos", "số"), ("thangs", "tháng"), ("cuwar", "cửa"), ("cuar", "của"), ("ddown", "đơn"), ("dduwowcj", "được"),
    ("tieenf", "tiền"), ("laf", "là"), ("nhaats", "nhất"), ("toongr", "tổng"), ("huyr", "huỷ"), ("gias", "giá"),
    ("quys", "quý"), ("nguwowif", "người"), ("Thangs", "Tháng"), ("ees", "ế"),
])
def test_telex_decodes(tok, word):
    assert decode(tok) == word


@pytest.mark.parametrize("tok", ["check", "plus", "kids", "max", "strepsils", "heineken", "insee", "dulux", "size", "bill"])
def test_telex_leaves_english_and_brands(tok):
    assert decode(tok) is None


def test_telex_protect():
    assert decode("bans") == "bán"
    assert decode("bans", protect={"bans"}) is None


@pytest.mark.parametrize("q,expected", [
    ("dthu th7/26 bnhiu z", "Doanh thu tháng 7/2026 bao nhiêu vậy"),
    ("revenue tuần rồi của cuwar hàng laf bnhiu", "Doanh thu tuần rồi của cửa hàng là bao nhiêu"),
    ("T6/2025 mặt hàng mô cho lời nhiều nhất?", "Tháng 6/2025 mặt hàng nào cho lời nhiều nhất?"),
    ("tháng này cửa hàng bán dc nhiêu tiền", "Tháng này cửa hàng bán được bao nhiêu tiền"),
    ("so vs tháng trc", "So với tháng trước"),
    ("có k bán dc 152k", "Có không bán được 152.000 đồng"),
    ("tuần ni quán bán bao nhiêu", "Tuần này quán bán bao nhiêu"),
    ("coi giùm 🙏 top 3 hàng hot nhất là j", "Coi giùm top 3 hàng bán chạy nhất là gì"),
])
def test_rules_normalize(q, expected):
    assert rules_normalize(q).text == expected


def test_clean_drops_emoji():
    assert clean("trog tháng 6  wán 🤔 ") == "trog tháng 6 wán"


@pytest.mark.parametrize("text,out,note", [
    ("từ 3tr7 trở lên", "từ 3.700.000 đồng trở lên", "'3tr7' = 3700000 VND"),
    ("1tr134", "1.134.000 đồng", "'1tr134' = 1134000 VND"),
    ("18 triệu", "18.000.000 đồng", "'18 triệu' = 18000000 VND"),
    ("1,5tr", "1.500.000 đồng", "'1,5tr' = 1500000 VND"),
    ("lên 152k/chai", "lên 152.000 đồng/chai", "'152k' = 152000 VND"),
])
def test_amounts(text, out, note):
    assert normalize_amounts(text) == (out, [note])


@pytest.mark.parametrize("text", ["Enterogermina 2 tỷ/5ml", "tháng 1/2026 tỉ lệ huỷ", "lên 44.000đ", "Efticol 0,9%"])
def test_amounts_untouched(text):
    assert normalize_amounts(text) == (text, [])


@pytest.mark.parametrize("text,out", [
    ("So sánh 3/25, 4/25 và 5/25.", "So sánh tháng 3/2025, tháng 4/2025 và tháng 5/2025."),
    ("th7/26", "tháng 7/2026"),
    ("6-2025 quán bán", "tháng 6/2025 quán bán"),
    ("quy 2/2025", "quý 2 năm 2025"),
    ("từ 28/5/25", "từ 28/5/2025"),
    ("ngày 5/12", "ngày 5/12"),
    ("ngày 02/07/2025", "ngày 02/07/2025"),
])
def test_dates(text, out):
    assert normalize_dates(text) == out


def _index():
    products = pd.DataFrame({"Tên hàng": ["Dầu gội Clear 630g", "Bạc xỉu (M)", "Gas Petrolimex 12kg"]})
    customers = pd.DataFrame({"Khách hàng": ["Đoàn Thảo Sơn", "Hứa Minh Thắng", "Khách lẻ"]})
    roles = {"p": {"Tên hàng": "product_name"}, "c": {"Khách hàng": "customer_name"}}
    return EntityIndex.from_tables({"p": products, "c": customers}, roles)


def test_entities_fix_names_and_protect_them():
    r = rules_normalize("khach doan thao son thang 1/2026 no them bn", _index())
    assert r.text == "Khach Đoàn Thảo Sơn tháng 1/2026 no them bao nhiêu"
    assert [(m.kind, m.name, m.full) for m in r.mentions] == [("customer", "Đoàn Thảo Sơn", True)]
    r = rules_normalize("Từ khi Bạc xỉu (M) lên 35.000 đồng/ly", _index())
    assert r.text == "Từ khi Bạc xỉu (M) lên 35.000 đồng/ly"


def test_entities_one_word_group_after_cue():
    groups = pd.DataFrame({"Nhóm hàng": ["Lẩu", "Bia"]})
    idx = EntityIndex.from_tables({"g": groups}, {})
    assert rules_normalize("ti trong cua nhom lau thang 6/2026", idx).text == "Ti trong cua nhom Lẩu tháng 6/2026"
    assert rules_normalize("lau qua chua ban", idx).mentions == []


def test_entities_respect_diacritics():
    # 'tháng' written with diacritics is not the customer 'Thắng'
    assert rules_normalize("Xem giúp mình tháng 12/2025 doanh thu", _index()).mentions == []


def test_entities_protect_brand_words_from_telex():
    assert "Gas" in rules_normalize("bán bao nhiêu bình Gas Petrolimex 12kg", _index()).text


def test_rewrite_parse_rejects_changed_numbers():
    rules = rules_normalize("dthu th7/26 bnhiu z")
    ok = parse('{"normalized": "Doanh thu tháng 7/2026 là bao nhiêu?", "answer_type": "number"}', rules)
    assert ok["normalized"] == "Doanh thu tháng 7/2026 là bao nhiêu?"
    bad = parse('{"normalized": "Doanh thu tháng 6/2026 là bao nhiêu?", "answer_type": "number"}', rules)
    assert bad["normalized"] is None and bad["answer_type"] == "number"
    assert parse("not json", rules) is None


def test_mentions_use_the_spelling_of_sales_lines():
    sales = pd.DataFrame({"product_name": ["Coca Cola 330ml"] * 2, "product_name_raw": ["Coca Cola 330ml", "COCA COLA 330ML"]})
    tables = {"hang": pd.DataFrame({"ten": ["COCA COLA 330ML"]})}
    index = EntityIndex.from_tables(tables, {"hang": {"ten": "product_name"}}, sales)
    assert [m.name for m in index.find("doanh thu coca cola 330ml") if m.full] == ["Coca Cola 330ml"]
