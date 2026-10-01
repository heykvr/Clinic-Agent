from __future__ import annotations

import os

from .base import LLM, AssistantTurn, with_retries


class AnthropicLLM(LLM):
    def __init__(self, model: str, temperature: float | None = None):
        import anthropic  # lazy: only needed when this provider is selected
        self.client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        self.temperature = temperature

    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"] or "(no message)"})
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "..."}]})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                # Anthropic wants all results for one assistant turn inside a single user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def chat(self, system, messages, tools=None, max_tokens=1024):
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=self._convert(messages))
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"],
                                "input_schema": t["parameters"]} for t in tools]
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        def call():
            try:
                return self.client.messages.create(**kwargs)
            except Exception as e:
                # Some models reject sampling params; degrade gracefully once.
                if "temperature" in str(e).lower() and "temperature" in kwargs:
                    kwargs.pop("temperature")
                    self.temperature = None
                    return self.client.messages.create(**kwargs)
                raise

        resp = with_retries(call)
        text, calls = [], []
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                calls.append({"id": b.id, "name": b.name, "args": dict(b.input or {})})
        return AssistantTurn("\n".join(text).strip(), calls)
