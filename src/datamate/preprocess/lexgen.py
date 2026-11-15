"""Development tool (needs the internet, never runs at inference): the generated part of the question lexicon.

Seeds are standard Vietnamese words and two-word phrases from the TRAIN split only (question words used by at least
8 train shops, the term cells of the train rulebook). Gemini lists how people type each seed in chat: abbreviations,
teen-code, dropped or swapped letters, regional words, English. Nothing of the public or private test is sent or read.
Every proposal passes automatic filters (lexicon.accept) before it lands in assets/lexicon_external.json, with its source;
the raw replies are kept next to it so the data can be audited and rebuilt (organizers' QnA: data made with a commercial
API is allowed and must be submitted).

    uv run python -m datamate.preprocess.lexgen            # rebuild assets/lexicon_external.json (--refilter: no API call; --verify: redo the check only)
"""

import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from datamate.agent.evidence import rule_units
from datamate.preprocess.lexicon import EXTERNAL_PATH, accept, train_vocab

MODEL = "gemini-3-flash-preview"
RAW_PATH = EXTERNAL_PATH.with_name("lexicon_gemini_raw.jsonl")
VERIFY_PATH = EXTERNAL_PATH.with_name("lexicon_gemini_verify.jsonl")
_JSON = re.compile(r"\[.*\]", re.S)

PROMPT = """Bạn là chuyên gia ngôn ngữ chat tiếng Việt. Chủ cửa hàng nhỏ (tạp hoá, quán cà phê, nhà thuốc, cửa hàng vật liệu,
nhà hàng...) nhắn tin hỏi trợ lý về số liệu bán hàng. Với mỗi từ hoặc cụm từ chuẩn dưới đây, liệt kê
các cách người Việt THỰC SỰ gõ nó khi nhắn tin: viết tắt, teencode, gõ sai/thiếu chữ phổ biến, từ địa phương (Bắc, Trung, Nam)
cùng nghĩa, từ tiếng Anh hay dùng. Không liệt kê dạng chỉ bỏ dấu (ví dụ "doanh thu" -> "doanh thu" không dấu). Chỉ liệt kê dạng
có thật và phổ biến; dạng có thể đọc thành một từ thông dụng khác nghĩa thì bỏ.
Trả về DUY NHẤT một mảng JSON: [{"variant": "<dạng gõ, chữ thường>", "standard": "<từ chuẩn>"}].

Từ chuẩn:
"""


# entries read off public web pages (not test data), checked by `accept` like the generated ones
WEB = [
    ("mytour.vn/vi/blog/bai-viet/kham-pha-bi-an-cua-khu-man-va-troc-tru.html",
     {"rứa": "thế", "nớ": "đó", "tê": "kia", "tề": "kìa", "chộ": "thấy", "mần": "làm", "nỏ": "không", "hấn": "nó",
      "nhít": "nhất", "cụng": "cũng", "bựa ni": "hôm nay"}),
    ("cellphones.com.vn/sforum/teencode-la-gi",
     {"zậy": "vậy", "zào": "vào", "ckưa": "chưa", "fải": "phải", "nzau": "nhau", "tkường": "thường"}),
]


# generated entries refused on review (meaning or a clash with the amount and product rules), not on any test data
REFUSED = {"item": "mặt hàng, not cái", "sale": "a sale is not a discount", "off": "too short and English",
           "hay sale": "as sale", "xị": "100 nghìn in amounts, read by the amount rules", "rổ giá": "not giá",
           "mấy dĩa": "a dish is not a portion", "nợ nần": "not the accounting term", "chiều sell": "fragment"}


def seeds(train: Path, min_shops: int = 8) -> list[str]:
    """Standard words and bigrams of the train questions (with a diacritic, used by min_shops shops) and the train
    rulebook's term cells."""
    shops: dict[str, set] = defaultdict(set)
    for line in (train / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            toks = re.findall(r"[^\W\d_]+", unicodedata.normalize("NFC", q["question"].lower()))
            for t in toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]:
                shops[t].add(q["business_id"])
    accented = [t for t, s in shops.items() if len(s) >= min_shops and not t.isascii()]
    terms = {unicodedata.normalize("NFC", t.lower()).strip("*` ") for _, t, _ in rule_units((train / "rulebook.md").read_text(encoding="utf-8")) if t}
    return sorted(set(accented) | {t for t in terms if len(t.split()) <= 3})


def generate(train: Path, batch: int = 60) -> list[dict]:
    from datamate.agent.llm_gemini import GeminiLLM

    llm = GeminiLLM(MODEL, max_tokens=12000)
    words = seeds(train)
    raw = []
    for i in range(0, len(words), batch):
        part = words[i:i + batch]
        reply = llm.generate("Chỉ trả về JSON.", [PROMPT + "\n".join(part)], [], 0.0, 0)
        raw.append({"model": MODEL, "seeds": part, "reply": reply})
        print(f"batch {i // batch + 1}: {len(part)} seeds", file=sys.stderr, flush=True)
    return raw


def proposals(raw: list[dict]) -> list[tuple[str, str]]:
    out = []
    for r in raw:
        m = _JSON.search(r["reply"])
        try:
            items = json.loads(m.group(0)) if m else []
        except ValueError:
            items = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("variant"), str) and isinstance(it.get("standard"), str):
                out.append((unicodedata.normalize("NFC", it["variant"].strip().lower()), unicodedata.normalize("NFC", it["standard"].strip())))
    return out


VERIFY = """Kiểm tra từng cặp (dạng gõ -> dạng chuẩn) dùng để chuẩn hoá tin nhắn tiếng Việt của chủ cửa hàng hỏi về số liệu
bán hàng. Một cặp ĐẠT khi cả bốn điều đúng:
1. dạng chuẩn là một từ hoặc cụm từ trọn vẹn, có nghĩa (không phải mảnh cắt từ một cụm dài hơn);
2. dạng gõ là cách người Việt thật sự gõ đúng từ đó khi nhắn tin (viết tắt, teencode, gõ sai, từ địa phương, tiếng Anh);
3. thay dạng gõ bằng dạng chuẩn KHÔNG đổi nghĩa câu (không phải từ gần nghĩa, không phải từ khác nghĩa như ca tối/ca chiều);
4. dạng gõ hầu như không bao giờ dùng cho một từ phổ biến khác (ví dụ "tr" có thể là triệu hoặc trăm: KHÔNG ĐẠT).
Trả về DUY NHẤT một mảng JSON các số thứ tự ĐẠT, ví dụ [0, 3, 7].

"""


def verify(pairs: list[tuple[str, str]], batch: int = 80) -> tuple[set[int], list[dict]]:
    """Indices of the pairs a second Gemini pass finds right, unambiguous and whole; and its raw replies."""
    from datamate.agent.llm_gemini import GeminiLLM

    llm = GeminiLLM(MODEL, max_tokens=12000)
    ok, raw = set(), []
    for i in range(0, len(pairs), batch):
        part = pairs[i:i + batch]
        lines = "\n".join(f"{j}. {v} -> {s}" for j, (v, s) in enumerate(part))
        reply = llm.generate("Chỉ trả về JSON.", [VERIFY + lines], [], 0.0, 0)
        raw.append({"model": MODEL, "pairs": part, "reply": reply})
        m = _JSON.search(reply)
        try:
            ok |= {i + j for j in json.loads(m.group(0)) if isinstance(j, int) and 0 <= j < len(part)} if m else set()
        except ValueError:
            pass
    return ok, raw


def main(train: Path = Path("data/train"), regenerate: bool = True) -> None:
    """regenerate=False refilters the saved raw replies without calling Gemini."""
    if regenerate or not RAW_PATH.exists():
        raw = generate(train)
        RAW_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in raw), encoding="utf-8")
    raw = [json.loads(line) for line in RAW_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    vocab = train_vocab(train)
    data, have = [], set()
    for url, pairs in WEB:
        for var, std in pairs.items():
            if accept(var, std, vocab) is None and var not in have:
                have.add(var)
                data.append({"from": var, "to": std, "source": f"https://{url}"})
    standards = set(seeds(train))
    cands, rejected = [], 0
    for var, std in proposals(raw):
        if accept(var, std, vocab, standards) or var in have or var in REFUSED:
            rejected += 1
            continue
        have.add(var)
        cands.append((var, std))
    if regenerate or not VERIFY_PATH.exists():  # second pass: meaning kept, unambiguous, a whole word
        ok, vraw = verify(cands)
        VERIFY_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in vraw), encoding="utf-8")
    passed = {(v, s) for r in (json.loads(line) for line in VERIFY_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
              for v, s in _passed(r)}
    kept = [(v, s) for v, s in cands if (v, s) in passed]
    data += [{"from": v, "to": s, "source": f"gemini:{MODEL}"} for v, s in kept]
    EXTERNAL_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"generated: {len(cands)} pass the filters ({rejected} refused), {len(kept)} pass the check; "
          f"{len(data)} entries in {EXTERNAL_PATH}")


def _passed(rec: dict) -> list[tuple[str, str]]:
    m = _JSON.search(rec["reply"])
    try:
        idx = json.loads(m.group(0)) if m else []
    except ValueError:
        idx = []
    return [tuple(rec["pairs"][j]) for j in idx if isinstance(j, int) and 0 <= j < len(rec["pairs"])]


if __name__ == "__main__":
    if "--verify" in sys.argv:  # keep the generated replies, redo the check
        VERIFY_PATH.unlink(missing_ok=True)
    main(regenerate=not {"--refilter", "--verify"} & set(sys.argv))
