"""Telex residue decoder: 'dduwowcj' -> 'được', 'soos' -> 'số', 'thangs' -> 'tháng', 'cuwar' -> 'của'.

People typing with the Telex input method switched off leave the key sequence in the text. A token is
decoded only when the result is a well-formed Vietnamese syllable (a known onset plus a rhyme seen in the
train corpus, assets/vi_rhymes.txt, built by `datamate build-lexicon`), so English words and brand names
('check', 'plus', 'kids') stay as they are. Tokens of the shop's own names ('gas', 'max') come in through
`protect` and are never decoded.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

RHYMES_PATH = Path(__file__).resolve().parents[3] / "assets" / "vi_rhymes.txt"
ONSETS = sorted(["ngh", "ng", "nh", "ch", "gh", "gi", "kh", "ph", "qu", "th", "tr", "b", "c", "d", "đ", "g", "h", "k",
                 "l", "m", "n", "p", "r", "s", "t", "v", "x", ""], key=len, reverse=True)
# standard rhymes that are rare in the train text
EXTRA_RHYMES = {"oe", "oen", "oet", "oay", "oac", "oach", "oanh", "oăn", "oăng", "oăt", "uya", "uyt", "uych", "uơ",
                "ưt", "ưn", "ưm", "ươp", "ươm", "uâng", "uây", "eng", "ec", "ach", "ênh", "uêch", "uênh"}

TONE_KEYS = {"s": "́", "f": "̀", "r": "̉", "x": "̃", "j": "̣"}
_TONE_MARKS = set(TONE_KEYS.values())
# order matters: 'uwow' / 'uow' before the single pairs
_MODIFIERS = [("uwow", "ươ"), ("uow", "ươ"), ("dd", "đ"), ("aa", "â"), ("ee", "ê"), ("oo", "ô"),
              ("aw", "ă"), ("ow", "ơ"), ("uw", "ư")]
_VOWELS = set("aăâeêioôơuưy")
_MODIFIED = set("ăâêôơư")
_CANDIDATE = re.compile(r"^[a-z]{2,8}$")
_HAS_MARKER = re.compile(r"dd|aa|ee|oo|aw|ow|uw|[aeiouy][a-z]*[sfrxj]$")
_RHYME = re.compile(r"[aăâeêioôơuưy]{1,3}(c|ch|m|n|ng|nh|p|t)?")
# English words that would decode into a real syllable ('max' -> 'mã'); brand words come in through `protect`
ENGLISH = {"max", "gas", "box", "mix", "tax", "sex", "bar", "car", "air", "bus", "yes", "fax", "jar", "far",
           "mar", "tour", "per", "lux", "fix", "six", "hex", "vox", "dar", "tar"}


def strip_tone(s: str) -> str:
    """Remove the tone mark but keep vowel modifiers: 'tiền' -> 'tiên', 'được' -> 'đươc'."""
    d = unicodedata.normalize("NFD", s)
    return unicodedata.normalize("NFC", "".join(c for c in d if c not in _TONE_MARKS))


def split_onset(syl: str) -> tuple[str, str]:
    """('ngh', 'iêng') for 'nghiêng'; 'gi'/'qu' are onsets only when a vowel follows ('gì' -> ('g', 'i'))."""
    for o in ONSETS:
        if syl.startswith(o) and (o not in ("gi", "qu") or syl[len(o):len(o) + 1] in _VOWELS):
            return o, syl[len(o):]
    return "", syl


@lru_cache(maxsize=1)
def rhymes() -> frozenset[str]:
    if not RHYMES_PATH.exists():
        return frozenset()
    return frozenset(RHYMES_PATH.read_text(encoding="utf-8").split()) | EXTRA_RHYMES


def is_syllable(syl: str) -> bool:
    """Toneless syllable (modifiers kept) = known onset + known rhyme."""
    _, rhyme = split_onset(syl)
    return rhyme in rhymes()


def build_rhymes(texts) -> list[str]:
    """Rhymes (toneless, modifiers kept) of every word with diacritics in `texts`."""
    out: set[str] = set()
    for t in texts:
        for w in re.findall(r"[^\W\d_]+", unicodedata.normalize("NFC", str(t)).lower()):
            if w.isascii() or len(w) > 7:
                continue
            _, rhyme = split_onset(strip_tone(w))
            if _RHYME.fullmatch(rhyme):
                out.add(rhyme)
    return sorted(out)


def place_tone(syl: str, tone: str) -> str:
    """Put a combining tone mark on the right vowel of a toneless syllable (new style: hoà, thuỷ)."""
    onset, rhyme = split_onset(syl)
    m = re.match(r"[aăâeêioôơuưy]+", rhyme)
    if not m:
        return syl
    cluster, final = m.group(0), rhyme[m.end():]
    mods = [k for k, c in enumerate(cluster) if c in _MODIFIED]
    if mods:
        pos = mods[-1]
    elif final or len(cluster) == 1:
        pos = len(cluster) - 1
    elif cluster in ("oa", "oe", "uy") or len(cluster) == 3:
        pos = 1
    else:
        pos = 0
    k = len(onset) + pos
    return unicodedata.normalize("NFC", syl[:k + 1] + tone + syl[k + 1:])


def decode(token: str, protect: set[str] | frozenset[str] = frozenset()) -> str | None:
    """The Vietnamese word a Telex key sequence stands for, or None if `token` is not one."""
    low = token.lower()
    if not _CANDIDATE.match(low) or not _HAS_MARKER.search(low) or low in ENGLISH or low in protect:
        return None
    if not rhymes() or is_syllable(low):
        return None
    s, tone = low, None
    if s[-1] in TONE_KEYS:
        s, tone = s[:-1], TONE_KEYS[s[-1]]
    for a, b in _MODIFIERS:
        s = s.replace(a, b)
    if s == low or "w" in s or not is_syllable(s):
        return None
    stop_final = s[-1:] in ("p", "t", "c") or s.endswith("ch")
    if tone and stop_final and tone not in (TONE_KEYS["s"], TONE_KEYS["j"]):  # stop finals only take sắc / nặng
        return None
    out = place_tone(s, tone) if tone else s
    return out[0].upper() + out[1:] if token[0].isupper() else out
