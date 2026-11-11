"""The rulebook lines a question's words point to, quoted next to the question.

The whole rulebook sits in the system message, where a small model loses the one rule a question needs among a hundred.
The lines here are found by word overlap with the question (syllable unigrams and bigrams, without diacritics, weighted
by how rare they are in the rulebook; a match in a rule's term cell counts double), from the rulebook of the split
being answered, so a new rulebook brings its own rules.
"""

import math
import re
from collections import Counter

from datamate.text import fold

_WORD = re.compile(r"[a-z0-9]+")
_ROW = re.compile(r"^\|(.+)\|\s*$")


def rule_units(rulebook: str) -> list[tuple[str, str, str]]:
    """(section, term, line) of every rule: a table row (term = its first cell) or a bullet or paragraph line."""
    out, section = [], ""
    for raw in rulebook.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        m = _ROW.match(line)
        if m:
            cells = [c.strip() for c in m.group(1).split("|")]
            if set("".join(cells)) <= set("-: ") or fold(cells[0]) in ("luat", "thuat ngu", "quy tac"):
                continue  # separator or header row
            out.append((section, cells[0], line))
        elif len(line) >= 30:
            out.append((section, "", line))
    return out


# question words every rule could share; a gram made only of these says nothing about the rule
_STOP = set(("la cua cho trong voi va cac mot co khong duoc dc bao nhieu nhiu bnhiu z vay nao thi ma nay do o tu den het ngay "
             "thang nam tuan so ban hang cua hang shop bi da se dang roi gi sao the nhu").split())


def _grams(text: str) -> set[str]:
    words = _WORD.findall(fold(text))
    pairs = {f"{a} {b}" for a, b in zip(words, words[1:]) if not (a in _STOP and b in _STOP)}
    return {w for w in words if w not in _STOP and not w.isdigit()} | pairs


def pick_rules(rulebook: str, question: str, k: int = 5) -> list[str]:
    """The k rule lines that share the rarest words with the question, best first; none when nothing shares a bigram."""
    units = rule_units(rulebook)
    if not units:
        return []
    grams = [(_grams(t), _grams(f"{s} {line}")) for s, t, line in units]
    df = Counter(g for _, body in grams for g in body)
    idf = {g: math.log(len(units) / n) for g, n in df.items()}
    q = _grams(question)
    scored = []
    for (section, _, line), (term, body) in zip(units, grams):
        hit = q & body
        if not any(" " in g for g in hit):
            continue
        score = sum(idf[g] * (2 if " " in g else 1) * (3 if g in term else 1) for g in hit)
        scored.append((score, section, line))
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0] if scored else 0
    return [f"[{s}] {line}" for score, s, line in scored[:k] if score >= top / 2]
