"""The question's frame (L4): the slots code can fill, in one typed record, shown to the model as a block.

metric: the catalog group (agent/lint.CATALOG) or semantic contract the question matches, with the macros of this shop that
compute it; periods: the resolved time expressions; entities and values: names and column values found in the shop's data;
shape: the expected answer type and size; capability: the fact the question needs and its state in this shop (L2);
residual: the question words nothing above explains. A frame with an empty residual is fully explained by code.
The frame is also what the result is checked against (consistency): the dates and names it holds must reach the query.
"""

import re
from dataclasses import asdict, dataclass, field

from datamate.agent.evidence import _STOP
from datamate.agent.lint import CATALOG
from datamate.eval.semantic import CHECKS
from datamate.text import fold

_HINT = re.compile(r'^- "(.*?)" -> .*? = (\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})')
_WORD = re.compile(r"[a-z]+")
# question words that carry no slot: asking, politeness, quantity words the shape already holds
_EMPTY = set(("cho hoi xem giup giup gium dum coi check minh em anh chi oi a nhi nhe vay ha ta voi tinh den tong cong "
              "cac nhung moi loai la bao nhieu may bn bnhiu nhieu nhiu nao gi ai co khong ko duoc dc ban hang cua hang "
              "quan shop tiem ngay thang nam tuan quy hom nay qua truoc sau dau cuoi het tu den trong ky gan nhat lien "
              "ra vao so luong tien dong").split()) | _STOP


# words of the quantity itself, for the contracts (catalog groups also consume their regex match and macro docs)
METRIC_WORDS = {"revenue": "doanh thu tien ban thu ve", "order_count": "don hoa don", "cancel_count": "huy don hoa don",
                "cancel_rate": "ti le ty le huy don phan tram", "avg_order_value": "trung binh binh quan gia tri don hoa don",
                "shift": "doanh thu don so don"}


@dataclass
class Frame:
    metric: str | None = None
    macros: list[str] = field(default_factory=list)
    periods: list[tuple[str, str, str]] = field(default_factory=list)  # (phrase, first day, last day)
    entities: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    shape: str | None = None
    capability: tuple[str | None, str | None] = (None, None)
    residual: list[str] = field(default_factory=list)

    def block(self) -> str:
        lines = ["## Frame of the question (filled by code from the question and this shop's data; follow it unless the "
                 "question clearly says otherwise)"]
        if self.metric:
            lines.append(f"- quantity: {self.metric}" + (f" (macro: {', '.join(self.macros)})" if self.macros else ""))
        for phrase, a, b in self.periods:
            lines.append(f'- period "{phrase}": {a} .. {b}')
        if self.entities:
            lines.append("- names: " + "; ".join(self.entities))
        if self.shape:
            lines.append(f"- answer: {self.shape}")
        cap, state = self.capability
        if cap:
            lines.append(f"- needs {cap}: {state} in this shop")
        lines.append("- words not explained above: " + (", ".join(self.residual) if self.residual else "none"))
        return "\n".join(lines)

    def to_json(self) -> dict:
        return asdict(self)


def build_frame(text: str, hints: list[str], entities: list[str], values: list[str], shape: str | None,
                capability: tuple[str | None, str | None], macros, docs: dict[str, str] | None = None,
                vocab: frozenset[str] = frozenset()) -> Frame:
    """docs: macro name -> its card line (sql/agent.MACROS): the words of the chosen macros count as explained.
    vocab: words of the split's rulebook (rulebook_words): a term the rulebook defines is not residual."""
    f = fold(text)
    metric, names, used = None, [], []
    for name, rx, ms in CATALOG:
        m = rx.search(f)
        if m and all(x in macros for x in ms[:1]):
            metric, names = name, [x for x in ms if x in macros]
            used.append(m.group(0))
            break
    if metric is None:
        check = next((c for c in CHECKS if c.match(f)), None)
        if check is not None and check.name in macros:
            metric, names = check.name, [check.name]
    periods = []
    for h in hints:
        m = _HINT.match(h)
        if m:
            periods.append(m.groups())
            used.append(fold(m.group(1)))
    used += [fold(e) for e in entities] + [fold(v.split(" = ", 1)[-1]) for v in values]
    rest = f
    for u in sorted(used, key=len, reverse=True):
        rest = rest.replace(u, " ")
    known = set(_WORD.findall(fold(" ".join([metric or "", METRIC_WORDS.get(metric or "", "")]
                                             + [(docs or {}).get(n, "") for n in names]))))
    residual = [w for w in dict.fromkeys(_WORD.findall(rest)) if w not in _EMPTY | known | vocab and len(w) > 1]
    return Frame(metric, names, periods, list(entities), list(values), shape, capability, residual)


def rulebook_words(rulebook: str) -> frozenset[str]:
    return frozenset(_WORD.findall(fold(rulebook)))


def consistency(frame: Frame, code: str | None) -> list[str]:
    """What the frame holds that the query does not use (L6 round-trip, without a model): a resolved period whose days
    are not in the query, a name not in the query. Only a feature: a query can reach a period another way."""
    if not code or code.lstrip().upper().startswith("UNANSWERABLE"):
        return []
    body = fold(code)
    out = [f"period {a}..{b}" for _, a, b in frame.periods if a not in code and b not in code]
    out += [f"name {e}" for e in frame.entities if fold(e) not in body]
    return out
