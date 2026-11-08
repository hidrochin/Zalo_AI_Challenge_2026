"""Parse a business README.md: per-file column descriptions and the data end date."""

import re
from dataclasses import dataclass, field

import pandas as pd

from datamate.text import fold

_SECTION = re.compile(r"^##\s+Cột trong\s+`([^`]+)`(?:\s*,\s*sheet\s+`([^`]+)`)?", re.M)
_COL = re.compile(r"^- \*\*(.+?)\*\*:\s*(.+)$", re.M)
_END = re.compile(r"có đến hết\s+(\d{1,2}/\d{1,2}/\d{4})")
_UNIT_RULE = re.compile(r"\b1 ([^\s=:;,.]+(?: [^\s=:;,.]+)?) ?= ?(\d+(?:[.,]\d+)?) ?([^\s;,.]+)")
_ORDER_KEY = re.compile(r"(?:các|những) dòng (?:có )?cùng \*\*([^*]+)\*\* là (?:một|1) (?:đơn|hoá đơn|hóa đơn|bill|phiếu)", re.I)


@dataclass
class Readme:
    text: str
    data_end: pd.Timestamp | None
    # (file, sheet or None) -> {column name: description}
    columns: dict[tuple[str, str | None], dict[str, str]] = field(default_factory=dict)
    order_keys: set[str] = field(default_factory=set)  # folded column names that group rows into one order

    def known_names(self) -> set[str]:
        return {fold(c) for cols in self.columns.values() for c in cols}

    def descs_for(self, file: str, sheet: str | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for (f, sh), cols in self.columns.items():
            if _file_matches(f, file) and (sh is None or sh == sheet):
                out.update(cols)
        return out


def unit_rules(text: str) -> list[tuple[str, str, float, str]]:
    """(product, unit, factor, base unit) for conversions written in prose: `- Coca: 1 thùng = 24 lon` or
    `- **Hàng theo bó**: A, B bán theo bó, 1 bó = 10 cây`."""
    out = []
    for line in text.splitlines():
        m = _UNIT_RULE.search(line)
        if m is None:
            continue
        head = re.sub(r"^\s*-\s*(\*\*[^*]*\*\*:?)?\s*", "", line[:m.start()]).strip(" :,")
        names = head.split(" bán theo ")[0].split(",") if " bán theo " in head else [head]
        factor = float(m.group(2).replace(",", "."))
        out += [(n.strip(), m.group(1), factor, m.group(3)) for n in names if n.strip()]
    return out


def _file_matches(spec: str, file: str) -> bool:
    """README file spec, possibly with placeholders (`BanHang_T<tháng>_<năm>.xlsx`), vs an actual file name."""
    if spec == file:
        return True
    if "<" not in spec:
        return False
    rx = "".join(".+?" if part.startswith("<") else re.escape(part) for part in re.split(r"(<[^>]*>)", spec))
    return re.fullmatch(rx, file) is not None


def parse_readme(text: str) -> Readme:
    m = _END.search(text)
    data_end = pd.to_datetime(m.group(1), dayfirst=True) if m else None
    r = Readme(text=text, data_end=data_end, order_keys={fold(m.group(1)) for m in _ORDER_KEY.finditer(text)})
    sections = list(_SECTION.finditer(text))
    for i, sec in enumerate(sections):
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        body = text[sec.end():end]
        nxt = body.find("\n## ")
        if nxt >= 0:
            body = body[:nxt]
        cols = {c.group(1).strip(): c.group(2).strip() for c in _COL.finditer(body)}
        r.columns[(sec.group(1), sec.group(2))] = cols
    return r
