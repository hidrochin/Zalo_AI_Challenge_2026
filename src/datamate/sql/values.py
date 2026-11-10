"""Which values of this shop's data a question names, found without a model call: the step between the question's words and
a SQL filter. A question says "trả bằng tiền mặt", "nhóm trà", "bán tại quầy"; the data says 'cash', 'Trà', 'store'. The
model used to filter product names that contain the word, which counts another group or none at all.

Each text column of `sales_all` with a few distinct values (a channel, a payment method, a product or customer group, a
brand, a size, a yes/no) is indexed. A question phrase matches a value by its folded words, or through a small vocabulary
of how Vietnamese shops write the common codes. The hints name the column and the exact value; for "nhóm X" that no
column holds, they say so, since the rulebook takes a group from a group column only.
"""

import re

from datamate.ingest.roles import header_key
from datamate.text import fold

MAX_VALUES = 80  # more distinct values than this is a name or free-text column, not a category
_SKIP_COLS = re.compile(r"^(order_id|date|status|counted|order_ok|is_goods|walkin|product_(key|name|name_raw|id)|"
                        r"customer(_key|_name|_name_raw|_id)?|order_ts|unit|base_unit)$|__norm$|__unit$")  # units: qty_unit
_UNIT_HEAD = re.compile(r"^(dvt|dv|don vi( tinh| co so| co ban| ban| nhap| lon)?|dv (co so|co ban|tinh|nho)|unit)$")
_STOP = {"khac", "co", "khong", "le", "moi", "cu", "tat ca", "cac", "nhom", "loai", "hang", "don", "ban", "mua", "ngay", "thang"}
# how a question says a common code: question phrase (folded) -> values a shop may write for it (folded)
SYNONYMS = {
    "tien mat": ("cash", "tm", "tien mat"),
    "chuyen khoan": ("transfer", "bank", "bank transfer", "ck", "chuyen khoan"),
    "ck": ("transfer", "bank", "ck", "chuyen khoan"),
    "vi dien tu": ("ewallet", "e wallet", "wallet", "momo", "zalopay", "vnpay", "vi dien tu"),
    "cod": ("cod", "thu ho", "thu ho cod"),
    "thu ho": ("cod", "thu ho"),
    "ghi no": ("debt", "credit", "ghi no", "no"),
    "quet the": ("card", "pos", "the"),
    "tai quay": ("store", "pos", "offline", "tai quay", "tai cua hang", "quay"),
    "tai cua hang": ("store", "pos", "offline", "tai cua hang"),
    "dien thoai": ("phone", "call", "dien thoai", "goi dien"),
    "zalo": ("zalo", "zalo oa"),
    "zalo oa": ("zalo oa", "zalo"),
    "facebook": ("facebook", "fb", "fanpage", "messenger"),
    "san thuong mai dien tu": ("marketplace", "ecommerce", "shopee", "lazada", "tiki", "tiktok shop", "san tmdt"),
    "san tmdt": ("marketplace", "ecommerce", "shopee", "lazada", "tiki", "tiktok shop", "san tmdt"),
    "giao hang": ("delivery", "ship", "giao hang"),
    "online": ("online", "web", "website", "app"),
    "mang ve": ("takeaway", "take away", "mang ve"),
    "tai cho": ("dine in", "tai cho", "tai ban"),
}
_GROUP_PHRASE = re.compile(r"\bnhom ((?:(?!(?:thang|tuan|quy|nam|ngay|trong|chiem|ban|co|la|duoc|cua|nao|tu|den|va|voi|thi|th|"
                           r"mua|nay|qua|hom|bao|may|bn|chi|dong|gop|o|so|tong|doanh|dthu|tien|thu|khach|don)\b)[a-z0-9]+ ?){1,3})")
SUBJECTIVE = re.compile(r"\b(ngon|trung thuc|than thien|dang tin( cay)?|tin cay|uy tin|kho tinh|de tinh|lanh nghe|tay nghe|quy men|"
                        r"chat luong|nhiet tinh|vui ve|lich su|tan tam|hai long|yeu thich|tot bung|de thuong|chu dao|niem no|"
                        r"thong minh|hieu biet|dep)\b(?:\s+\w+){0,2}\s+nhat\b")


def _words(v: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(v)).split())


def _tokens(v: str) -> list[str]:
    """Lowercase words with their diacritics: 'trả' and 'trà' fold alike but are different words."""
    return [t for t in re.split(r"[^\w]+|_", str(v).lower()) if t]


class ValueIndex:
    """Text columns of the sales lines with few values: column -> {folded value: value as written}."""

    def __init__(self, columns: dict[str, dict[str, str]]):
        self.columns = columns

    @classmethod
    def from_store(cls, con) -> "ValueIndex":
        cols = con.execute("select column_name from duckdb_columns() where table_name = 'sales_all' "
                           "and data_type = 'VARCHAR'").fetchall()
        out: dict[str, dict[str, str]] = {}
        for (c,) in cols:
            if _SKIP_COLS.search(c) or _UNIT_HEAD.match(header_key(c)):
                continue
            vals = con.execute(f'select distinct "{c}" from sales_all where "{c}" is not null limit {MAX_VALUES + 1}').fetchall()
            if 1 < len(vals) <= MAX_VALUES:
                out[c] = {_words(v): v for (v,) in vals if isinstance(v, str) and _words(v)}
        return cls(out)

    def _find(self, phrase: str, said: list[str]) -> list[tuple[str, str]]:
        """(column, value) whose folded words equal the phrase or one of its codes. said: the question's own words there.
        A question written with diacritics must match them ('trả' is not 'trà'); one written without matches by folding,
        except a one-word value of 3 letters or fewer, which too many other words fold to."""
        codes = set(SYNONYMS.get(phrase, ()))
        plain = all(t == fold(t) for t in said)
        out = []
        for c, vals in self.columns.items():
            for w, v in vals.items():
                if w in codes:
                    out.append((c, v))
                elif w == phrase and (_tokens(v) == said if not plain else (" " in w or len(w) > 3)):
                    out.append((c, v))
        return out

    def hints(self, text: str, names: tuple[str, ...] = ()) -> list[str]:
        """Prompt lines: the column and value behind each phrase of the question that names one."""
        said = _tokens(text)
        folded = [_words(t) for t in said]
        for n in names:  # an item or customer named in full is no category word
            nt = [_words(t) for t in _tokens(n)]
            for i in range(len(folded) - len(nt) + 1):
                if nt and folded[i:i + len(nt)] == nt:
                    folded[i:i + len(nt)] = [""] * len(nt)
        f = " ".join(folded)
        out, seen = [], set()
        phrases = {w for vals in self.columns.values() for w in vals if len(w) > 2 and w not in _STOP}
        phrases |= set(SYNONYMS)
        taken = [False] * len(folded)
        for p in sorted(phrases, key=len, reverse=True):
            pw = p.split()
            for i in range(len(folded) - len(pw) + 1):
                if folded[i:i + len(pw)] != pw or any(taken[i:i + len(pw)]):
                    continue
                found = self._find(p, said[i:i + len(pw)])
                if found:
                    taken[i:i + len(pw)] = [True] * len(pw)
                    seen.add(p)
                    out.append(f'- "{" ".join(said[i:i + len(pw)])}" -> ' + "; ".join(f'"{c}" = \'{v}\'' for c, v in found[:4]))
                break
        for m in _GROUP_PHRASE.finditer(f):
            g = " ".join(m.group(1).split())
            if not g or any(g == s or g.startswith(s + " ") or s.startswith(g + " ") for s in seen):
                continue
            i = len(f[:m.start(1)].split())
            exact = self._find(g, said[i:i + len(g.split())])
            near = [(c, v) for c, vals in self.columns.items() for w, v in vals.items()
                    if re.search(rf"\b{re.escape(g)}\b", w) and (c, v) not in exact]
            label = "nhóm " + " ".join(said[i:i + len(g.split())])
            if exact:
                out.append(f'- "{label}" -> ' + "; ".join(f'"{c}" = \'{v}\'' for c, v in exact[:3])
                           + (f"; a different group, not counted: {', '.join(repr(v) for _, v in near[:4])}" if near else ""))
            elif near:
                out.append(f'- "{label}": no value is exactly that; values containing it: '
                           + "; ".join(f'"{c}" = \'{v}\'' for c, v in near[:5]))
            else:
                out.append(f'- "{label}": no column of this shop holds it as a value. The rulebook takes a group from the '
                           "group column, never from words in product names: with no such column the answer is UNANSWERABLE.")
            seen.add(g)
        return out


def subjective_note(text: str, rulebook: str, names: tuple[str, ...] = ()) -> str | None:
    """A superlative of a quality the data cannot measure ('khách trung thực nhất', 'món ngon nhất') that the rulebook does
    not define: the rulebook's unanswerable case for subjective questions."""
    f = fold(text)
    for n in names:
        f = f.replace(fold(n), " ")
    m = SUBJECTIVE.search(f)
    if m is None or re.search(rf"\b{m.group(1)}\b", fold(rulebook)):
        return None
    return (f'"{m.group(0)}" asks for a quality no column measures and the rulebook does not define: such a question is '
            "UNANSWERABLE (subjective), unless the question itself says how to measure it.")
