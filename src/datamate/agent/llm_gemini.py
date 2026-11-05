"""Gemini through its OpenAI-compatible endpoint: development only, never part of a submission.

The key is GEMINI_API_TOKEN in the environment or in .env.
"""

import os
from pathlib import Path

from openai import OpenAI

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_TOKEN")
    if key:
        return key
    env = Path(".env")
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        if line.startswith("GEMINI_API_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("GEMINI_API_TOKEN is not set.")


class GeminiLLM:
    def __init__(self, model: str, max_tokens: int = 12000, timeout: float = 300.0):
        self.client = OpenAI(base_url=BASE_URL, api_key=_api_key(), timeout=timeout, max_retries=0)
        self.model = model
        self.max_tokens = max_tokens

    def generate(self, system: str, user_parts: list[str], history: list[dict], temperature: float, seed: int,
                 max_tokens: int | None = None) -> str:
        """The endpoint rejects `seed`, and thinking counts as output, so the caller's smaller limit is not applied."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}, *history]
        r = self.client.chat.completions.create(model=self.model, messages=messages, temperature=temperature,
                                                max_tokens=self.max_tokens)
        return r.choices[0].message.content or ""
