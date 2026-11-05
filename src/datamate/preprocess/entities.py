"""Link the names a question mentions to the exact spelling in the shop's data.

Questions write names without diacritics or in lowercase ('khach doan thao son', 'dau goi clear 630g').
The index holds every product, customer, staff, supplier and group name of one business; a name matches
when its folded tokens occur contiguously in the folded question. A full match gives the exact spelling;
a partial one (most of a long product name) is listed as a candidate for the model to check.
"""

import math
import re
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from datamate.text import fold

_TOKEN = re.compile(r"\w+")
_KIND_HEADERS = [  # folded header -> kind, for columns without a role
    ("staff", re.compile(r"nhan vien|thu ngan|nguoi (ban|lap|thu)|ky thuat|\btho\b|\bnv\b|staff|cashier")),
    ("supplier", re.compile(r"nha cung cap|\bncc\b|supplier")),
    ("group", re.compile(r"^(nhom|loai|danh muc|nganh)( hang| san pham| sp| mon| thuoc)?$|^nhom|hoat chat|thuong hieu|hang sx")),
]
_ROLE_KIND = {"product_name": "product", "customer_name": "customer", "product_id": "code", "customer_id": "code"}
# single-word names only count after a word that introduces a person ('thợ Đạt', 'nhân viên Lan')
_GROUP_CUE = {"nhom", "loai", "nganh", "muc"}  # 'nhóm lẩu': one-word group names
_PERSON_CUE = {"nhan", "vien", "nv", "tho", "anh", "chi", "a", "c", "em", "ban", "khach", "kh", "co", "chu", "ba", "ong"}
_WALKIN = re.compile(r"^(khach (le|vang lai)|le|vang lai|khach)$|^(tong|cong|total)\b")  # walk-ins, subtotal labels
_PARTIAL_OK = {"product", "group"}  # a person's name must match in full


@dataclass
class Mention:
    kind: str
    name: str
    start: int  # character span in the question
    end: int
    full: bool  # every token of the name is in the question


def _tokens(s: str) -> list[str]:
    return [fold(t) for t in _TOKEN.findall(str(s))]


def _same(q_low: str, q_fold: str, n_low: str, n_fold: str) -> bool:
    """A question word written with diacritics must match them ('tháng' is not 'Thắng'); one without may match any."""
    return q_low == n_low or (q_low.isascii() and q_fold == n_fold)


class EntityIndex:
    def __init__(self, names: dict[str, set[str]]):
        self.entries: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []  # kind, name, folded, lowercase tokens
        seen = set()
        for kind, vals in names.items():
            for v in vals:
                toks = tuple(_tokens(v))
                if not toks or (kind, toks) in seen or _WALKIN.match(" ".join(toks)):
                    continue
                if kind != "code" and not any(len(t) > 1 for t in toks):
                    continue
                if kind == "code" and not (len(toks) == 1 and re.search(r"[a-z]", toks[0]) and re.search(r"\d", toks[0])):
                    continue  # item codes look like 'SP0012'; bare numbers would match periods and amounts
                seen.add((kind, toks))
                self.entries.append((kind, str(v).strip(), toks, tuple(t.lower() for t in _TOKEN.findall(str(v)))))
        df: dict[str, int] = defaultdict(int)
        self.by_token: dict[str, list[int]] = defaultdict(list)
        for i, (_, _, toks, _) in enumerate(self.entries):
            for t in set(toks):
                df[t] += 1
                self.by_token[t].append(i)
        n = max(len(self.entries), 1)
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.vocab = frozenset(t for t in df if not t.isdigit())

    @classmethod
    def from_tables(cls, tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict], sales: pd.DataFrame | None = None,
                    max_unique: int = 50_000) -> "EntityIndex":
        names: dict[str, set[str]] = defaultdict(set)
        frames = [(n, df, roles_by_table.get(n, {})) for n, df in tables.items()]
        if sales is not None:
            frames.append(("sales_lines", sales, {"product_name": "product_name", "customer_name": "customer_name"}))
        for _, df, roles in frames:
            for col in df.columns:
                if not (df[col].dtype == object or pd.api.types.is_string_dtype(df[col])):
                    continue
                kind = _ROLE_KIND.get(roles.get(col, ""))
                if kind is None:
                    h = fold(str(col).replace("_", " "))
                    kind = next((k for k, rx in _KIND_HEADERS if rx.search(h)), None)
                if kind is None:
                    continue
                u = df[col].dropna().astype(str).unique()
                if len(u) <= max_unique:
                    names[kind].update(x for x in u if 1 < len(x) <= 120)
        return cls(names)

    def find(self, question: str, max_per_span: int = 3) -> list[Mention]:
        words = list(_TOKEN.finditer(question))
        qlow = [m.group(0).lower() for m in words]
        qtok = [fold(w) for w in qlow]
        # candidates share at least one fairly rare token with the question ('clear', 'son'; not 'hop', 'chai')
        cands = {i for t in set(qtok) if 0 < len(self.by_token.get(t, ())) <= 300 for i in self.by_token[t]}
        found: list[tuple[float, int, int, int, bool]] = []  # score, start tok, end tok, entry, full
        for i in cands:
            kind, name, toks, low = self.entries[i]
            run, at = self._longest_run(toks, low, qtok, qlow)
            if run == 0:
                continue
            full = run == len(toks)
            if not full and kind not in _PARTIAL_OK:
                continue
            cue = qtok[at - 1] if at > 0 else ""
            if len(toks) == 1 and kind != "code" and not (
                    (kind in ("staff", "customer") and cue in _PERSON_CUE) or (kind == "group" and cue in _GROUP_CUE)):
                continue
            cov = self._coverage(toks, qtok[at:at + run])
            if not full and (run < 2 or cov < 0.6):
                continue
            found.append((cov + (1 if full else 0) + 0.01 * run, at, at + run, i, full))
        found.sort(key=lambda x: (-x[0], x[1]))
        out: list[Mention] = []
        per_span: dict[tuple[int, int], int] = defaultdict(int)
        taken: list[tuple[int, int]] = []
        for score, a, b, i, full in found:
            if any(a < y and x < b for x, y in taken) and (a, b) not in per_span:
                continue  # overlaps a better, different span
            if per_span[(a, b)] >= max_per_span:
                continue
            per_span[(a, b)] += 1
            taken.append((a, b))
            kind, name, _, _ = self.entries[i]
            start, end = words[a].start(), words[b - 1].end()
            tail = re.search(r"[^\w\s]+$", name)  # 'Bạc xỉu (M)': take the ')' along
            if full and tail and question[end:end + len(tail.group(0))] == tail.group(0):
                end += len(tail.group(0))
            out.append(Mention(kind, name, start, end, full))
        return sorted(out, key=lambda m: (m.start, not m.full))

    def _coverage(self, toks: tuple[str, ...], matched: list[str]) -> float:
        tot = sum(self.idf.get(t, 0) for t in toks) or 1.0
        return sum(self.idf.get(t, 0) for t in set(matched)) / tot

    @staticmethod
    def _longest_run(toks, low, qtok, qlow) -> tuple[int, int]:
        """Longest contiguous run of name tokens that occurs in the question: (length, question token index)."""
        best, at = 0, -1
        for qi in range(len(qtok)):
            for ti in range(len(toks)):
                k = 0
                while (qi + k < len(qtok) and ti + k < len(toks)
                       and _same(qlow[qi + k], qtok[qi + k], low[ti + k], toks[ti + k])):
                    k += 1
                if k > best:
                    best, at = k, qi
        return best, at
