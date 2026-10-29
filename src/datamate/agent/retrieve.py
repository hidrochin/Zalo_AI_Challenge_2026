"""Few-shot example retrieval: char n-gram TF-IDF over accent-folded questions (no model parameters)."""

import json
import re
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from datamate.text import fold


def _key(q: str) -> str:
    return re.sub(r"\d+", "0", fold(q))


class ExampleBank:
    def __init__(self, items: list[dict]):
        self.items = items
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True)
        self.mat = self.vec.fit_transform([_key(it["question"]) for it in items]) if items else None
        self.biz = np.array([it["business_id"] for it in items])
        self.has_code = np.array([bool(it.get("code")) for it in items])

    @classmethod
    def from_train(cls, train_dir: Path, examples_path: Path | None = None) -> "ExampleBank":
        """Train questions + gold answers; verified programs from `examples_path` (jsonl) are attached when present."""
        train_dir = Path(train_dir)
        qs = {}
        for line in (train_dir / "questions.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                q = json.loads(line)
                qs[q["id"]] = q
        items = []
        for line in (train_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                a = json.loads(line)
                if a["id"] in qs:
                    items.append({**qs[a["id"]], "type": a["type"], "answer": a.get("answer")})
        if examples_path and Path(examples_path).exists():
            code = {}
            for line in Path(examples_path).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = json.loads(line)
                    code[e["id"]] = e["code"]
            for it in items:
                if it["id"] in code:
                    it["code"] = code[it["id"]]
        return cls(items)

    def query(self, question: str, exclude_business: str | None, k: int = 4) -> list[dict]:
        if not self.items or k <= 0:
            return []
        sims = (self.mat @ self.vec.transform([_key(question)]).T).toarray().ravel()
        sims = sims + 0.05 * self.has_code
        if exclude_business is not None:
            sims[self.biz == exclude_business] = -1
        out, seen = [], set()
        for i in np.argsort(-sims):
            if sims[i] < 0 or len(out) >= k:
                break
            key = _key(self.items[i]["question"])
            if key in seen:
                continue
            seen.add(key)
            out.append(self.items[i])
        return out
