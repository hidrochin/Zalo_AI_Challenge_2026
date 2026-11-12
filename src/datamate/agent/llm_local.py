"""Local models through an OpenAI-compatible server (vLLM, Ollama, LM Studio, llama.cpp).

This is the backend used in the competition: everything runs offline on our own GPUs.
"""

import copy
import json
import threading
import urllib.request

from openai import OpenAI

_trace = threading.local()  # reasoning of each model call made on this thread, for the per-question log


def start_trace() -> None:
    _trace.calls = []


def take_trace() -> list[str] | None:
    """The reasoning of every call since start_trace(), in call order ("" for a call without any); None if none had any."""
    calls, _trace.calls = getattr(_trace, "calls", None), None
    return calls if calls and any(calls) else None


def _is_ollama(base_url: str) -> bool:
    """Ollama answers /api/version next to its /v1 API; vLLM and the others do not."""
    root = str(base_url).rstrip("/").removesuffix("/v1")
    try:
        with urllib.request.urlopen(f"{root}/api/version", timeout=3) as r:
            return "version" in json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False


class LocalLLM:
    def __init__(self, base_url: str, model: str, max_tokens: int = 2048, enable_thinking: bool | None = None,
                 api_key: str = "EMPTY", timeout: float = 900.0, effort: str = "medium"):
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.effort = effort
        self.ollama = enable_thinking is not None and _is_ollama(base_url)

    def without_thinking(self) -> "LocalLLM":
        """The same server and model with thinking off: short calls (rewrite, column roles) whose budget thinking would eat."""
        if not self.enable_thinking:
            return self
        aux = copy.copy(self)
        aux.enable_thinking = False
        return aux

    def generate(self, system: str, user_parts: list[str], history: list[dict], temperature: float, seed: int,
                 max_tokens: int | None = None) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}]
        messages += history
        extra = {}
        if self.enable_thinking is not None:  # Qwen3-style chat templates on vLLM
            extra["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
            if self.ollama:  # Ollama ignores the template kwargs; its thinking switch is reasoning_effort
                extra["reasoning_effort"] = self.effort if self.enable_thinking else "none"
        r = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature, seed=seed,
            max_tokens=max_tokens or self.max_tokens, extra_body=extra or None,
        )
        msg = r.choices[0].message
        if getattr(_trace, "calls", None) is not None:  # vLLM's reasoning parser: `reasoning` (newer) or `reasoning_content`
            extra_fields = msg.model_extra or {}
            _trace.calls.append(extra_fields.get("reasoning") or extra_fields.get("reasoning_content") or "")
        return msg.content or ""
