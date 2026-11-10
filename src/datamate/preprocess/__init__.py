"""Question preprocessing: deterministic rules first, then one LLM rewrite into standard Vietnamese.

    raw question
      -> clean        NFC, emoji and stray symbols removed, whitespace collapsed
      -> telex        'soos' -> 'số', 'dduwowcj' -> 'được'           (telex.py)
      -> entities     shop names found and protected from the rewrites (entities.py)
      -> words        'dthu' -> 'doanh thu', 'mô' -> 'nào', 'revenue' -> 'doanh thu'  (lexicon.py)
      -> amounts      '3tr7' -> '3.700.000 đồng'; 'th7/26' -> 'tháng 7/2026'        (amounts.py)
      = rules text    used for time hints, retrieval and answer-type expectations
      -> LLM rewrite  standard Vietnamese + answer type, top-k, sub-questions          (rewrite.py)

The solver prompt shows the original question next to the normalized one, so a bad rewrite can be
overruled by the model.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from datamate.preprocess.amounts import normalize_amounts, normalize_dates
from datamate.preprocess.entities import EntityIndex, Mention
from datamate.preprocess.lexicon import PHRASES, PHRASES3, WORDS, contextual
from datamate.preprocess.telex import decode
from datamate.text import norm_key

__all__ = ["EntityIndex", "Mention", "RulesResult", "clean", "rules_normalize"]

_WORD = re.compile(r"\w+")
_PH = 0xE000  # private-use code points stand in for protected name spans


@dataclass
class RulesResult:
    text: str
    changes: list[str] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)


def clean(text: str) -> str:
    """NFC; drop emoji, pictographs, variation selectors and zero-width characters; collapse whitespace."""
    s = unicodedata.normalize("NFC", str(text))
    s = "".join(c for c in s if unicodedata.category(c) not in ("So", "Cf", "Co", "Cs") and not 0xFE00 <= ord(c) <= 0xFE0F)
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return " ".join(s.split())


def _cap(src: str, rep: str) -> str:
    return rep[0].upper() + rep[1:] if src[:1].isupper() and rep else rep


def _rewrite_words(text: str, changes: list[str]) -> str:
    words = list(_WORD.finditer(text))
    low = [m.group(0).lower() for m in words]
    out, last, i = [], 0, 0
    while i < len(words):
        m, w = words[i], low[i]
        nxt = low[i + 1] if i + 1 < len(words) else None
        rep, span = None, 1
        nxt2 = low[i + 2] if i + 2 < len(words) else None
        if nxt2 is not None and (w, nxt, nxt2) in PHRASES3:
            rep, span = PHRASES3[(w, nxt, nxt2)], 3
        elif nxt is not None and (w, nxt) in PHRASES:
            rep, span = PHRASES[(w, nxt)], 2
        elif w in WORDS:
            rep = WORDS[w]
        else:
            rep = contextual(low[i - 1] if i else None, w, nxt, low[i + 2] if i + 2 < len(words) else None,
                             i == len(words) - 1)
        if rep is not None:
            end = words[i + span - 1].end()
            src = text[m.start():end]
            out.append(text[last:m.start()] + _cap(src, rep))
            changes.append(f"{src} -> {rep}")
            last = end
        i += span
    return "".join(out) + text[last:]


def rules_normalize(question: str, index: EntityIndex | None = None) -> RulesResult:
    changes: list[str] = []
    text = clean(question)
    protect = index.vocab if index is not None else frozenset()

    def telex(m):
        d = decode(m.group(0), protect)
        if d is None:
            return m.group(0)
        changes.append(f"{m.group(0)} -> {d}")
        return d

    text = _WORD.sub(telex, text)

    mentions = index.find(text) if index is not None else []
    slots: list[str] = []
    if mentions:  # replace each protected span by one private-use character
        spans: dict[tuple[int, int], list[Mention]] = {}
        for mt in mentions:
            spans.setdefault((mt.start, mt.end), []).append(mt)
        pieces, last = [], 0
        for (a, b), ms in sorted(spans.items()):
            if a < last:
                continue
            full = {norm_key(x.name): x.name for x in ms if x.full}
            shown = text[a:b]
            if len(full) == 1 and norm_key(shown) not in full:  # only fix diacritics, keep the asker's casing
                shown = next(iter(full.values()))
            if shown != text[a:b]:
                changes.append(f"{text[a:b]} -> {shown}")
            pieces.append(text[last:a] + chr(_PH + len(slots)))
            slots.append(shown)
            last = b
        text = "".join(pieces) + text[last:]

    text = _rewrite_words(text, changes)
    text, amounts = normalize_amounts(text)
    for k, shown in enumerate(slots):
        text = text.replace(chr(_PH + k), shown)
    dated = normalize_dates(text)  # after the names are back: 'tháng' inside a span still counts as the prefix
    if dated != text:
        changes.append("dates normalized")
    text = dated
    text = " ".join(text.split())
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return RulesResult(text, changes, mentions, amounts)
