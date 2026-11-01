"""Local models through an OpenAI-compatible server (vLLM, Ollama, LM Studio, llama.cpp).

This is the backend used in the competition: everything runs offline on our own GPUs.
"""

from openai import OpenAI


class LocalLLM:
    def __init__(self, base_url: str, model: str, max_tokens: int = 2048, enable_thinking: bool | None = None,
                 api_key: str = "EMPTY", timeout: float = 900.0):
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking

    def generate(self, system: str, user_parts: list[str], history: list[dict], temperature: float, seed: int) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}]
        messages += history
        extra = {}
        if self.enable_thinking is not None:  # Qwen3-style chat templates on vLLM
            extra["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
            if not self.enable_thinking and ":11434" in str(self.client.base_url):  # Ollama ignores the template kwargs
                extra["reasoning_effort"] = "none"
        r = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature, seed=seed,
            max_tokens=self.max_tokens, extra_body=extra or None,
        )
        return r.choices[0].message.content or ""
