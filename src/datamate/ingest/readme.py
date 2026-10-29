"""Parse a business README.md: per-file column descriptions and the data end date."""

import re
from dataclasses import dataclass, field

import pandas as pd

from datamate.text import fold

_SECTION = re.compile(r"^##\s+Cột trong\s+`([^`]+)`(?:\s*,\s*sheet\s+`([^`]+)`)?", re.M)
_COL = re.compile(r"^- \*\*(.+?)\*\*:\s*(.+)$", re.M)
_END = re.compile(r"có đến hết\s+(\d{1,2}/\d{1,2}/\d{4})")


@dataclass
class Readme:
    text: str
    data_end: pd.Timestamp | None
    # (file, sheet or None) -> {column name: description}
    columns: dict[tuple[str, str | None], dict[str, str]] = field(default_factory=dict)

    def known_names(self) -> set[str]:
        return {fold(c) for cols in self.columns.values() for c in cols}

    def descs_for(self, file: str, sheet: str | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for (f, sh), cols in self.columns.items():
            if f == file and (sh is None or sh == sheet):
                out.update(cols)
        return out


def parse_readme(text: str) -> Readme:
    m = _END.search(text)
    data_end = pd.to_datetime(m.group(1), dayfirst=True) if m else None
    r = Readme(text=text, data_end=data_end)
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
