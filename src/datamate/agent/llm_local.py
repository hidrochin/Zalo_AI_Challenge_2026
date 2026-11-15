"""Local models through an OpenAI-compatible server (vLLM, Ollama, LM Studio, llama.cpp).

This is the backend used in the competition: everything runs offline on our own GPUs.
"""

import copy
import json
import urllib.request

from openai import OpenAI


def _is_ollama(base_url: str) -> bool:
    """Ollama answers /api/version next to its /v1 API; vLLM and the others do not."""
    root = str(base_url).rstrip("/").removesuffix("/v1")
    try:
        with urllib.request.urlopen(f"{root}/api/version", timeout=3) as r:
            return "version" in json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False


class Reply(str):
    """The reply text, as every caller reads it, with what the log needs besides: the model's thinking (vLLM's reasoning
    parser puts it apart from the content) and why generation stopped ('length' = the token budget ran out)."""

    reasoning: str | None = None
    finish_reason: str | None = None


def reply_of(choice) -> Reply:
    msg = choice.message
    extra = getattr(msg, "model_extra", None) or {}
    out = Reply(msg.content or "")
    out.reasoning = getattr(msg, "reasoning_content", None) or extra.get("reasoning_content") or extra.get("reasoning")
    out.finish_reason = choice.finish_reason
    return out


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

    def with_thinking(self, max_tokens: int) -> "LocalLLM":
        """The same server and model with thinking on and a budget that holds it: the deep tier of the router."""
        deep = copy.copy(self)
        deep.enable_thinking, deep.max_tokens = True, max_tokens
        return deep

    def generate(self, system: str, user_parts: list[str], history: list[dict], temperature: float, seed: int,
                 max_tokens: int | None = None) -> Reply:
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
        return reply_of(r.choices[0])
