"""Claude backend: development only, train split only.

Used to build the verified example bank and as an upper bound to tell ingestion bugs from
local-model mistakes. Never used at inference and never on public/private test data (rules 3 and 6).
"""

import anthropic


class ClaudeLLM:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", max_tokens: int = 16000):
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def generate(self, system: str, user_parts: list[str], history: list[dict], temperature: float, seed: int,
                 max_tokens: int | None = None) -> str:
        # temperature/seed are not accepted by current Claude models; sampling is the model's own.
        content = [{"type": "text", "text": p} for p in user_parts]
        content[0]["cache_control"] = {"type": "ephemeral"}  # README + schema card, shared per business
        messages = [{"role": "user", "content": content}] + history
        r = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max(max_tokens or self.max_tokens, 2048),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if r.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in r.content if b.type == "text")
