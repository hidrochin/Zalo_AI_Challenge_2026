"""Few-shot example retrieval: char n-gram TF-IDF over accent-folded questions (no model parameters)."""

import json
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from datamate.text import fold


def _key(q: str) -> str:
    return re.sub(r"\d+", "0", fold(q))


class ExampleBank:
    def __init__(self, items: list[dict], normalize: Callable[[str], str] | None = None):
        """normalize: applied to bank and query questions alike (e.g. the rule-based question cleaner)."""
        self.items = items
        self.normalize = normalize or (lambda s: s)
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True)
        self.keys = [_key(self.normalize(it["question"])) for it in items]
        self.mat = self.vec.fit_transform(self.keys) if items else None
        self.biz = np.array([it["business_id"] for it in items])
        self.has_code = np.array([bool(it.get("code")) for it in items])

    @classmethod
    def from_train(cls, train_dir: Path, examples_path: Path | None = None,
                   normalize: Callable[[str], str] | None = None) -> "ExampleBank":
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
        return cls(items, normalize)

    def _sims(self, question: str, exclude_business: str | None) -> np.ndarray:
        sims = (self.mat @ self.vec.transform([_key(self.normalize(question))]).T).toarray().ravel()
        if exclude_business is not None:
            sims[self.biz == exclude_business] = -1
        return sims

    def nearest(self, question: str, exclude_business: str | None) -> float:
        """Cosine similarity of the closest train question of another shop: how familiar the question's wording is."""
        return float(self._sims(question, exclude_business).max()) if self.items else 0.0

    def query(self, question: str, exclude_business: str | None, k: int = 4, drop: Callable[[dict], bool] | None = None) -> list[dict]:
        """drop(item): examples the target shop cannot use (a fact it lacks), skipped."""
        if not self.items or k <= 0:
            return []
        sims = self._sims(question, exclude_business)
        sims = np.where(sims < 0, sims, sims + 0.05 * self.has_code)
        out, seen = [], set()
        for i in np.argsort(-sims):
            if sims[i] < 0 or len(out) >= k:
                break
            key = self.keys[i]
            if key in seen or (drop is not None and drop(self.items[i])):
                continue
            seen.add(key)
            out.append(self.items[i])
        return out
