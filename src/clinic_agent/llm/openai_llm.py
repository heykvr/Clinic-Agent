from __future__ import annotations

import json
import os

from .base import LLM, AssistantTurn, with_retries


class OpenAILLM(LLM):
    def __init__(self, model: str, temperature: float | None = None):
        import openai  # lazy
        self.client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"),
                                    base_url=os.environ.get("OPENAI_BASE_URL") or None)
        self.model = model
        self.temperature = temperature

    @staticmethod
    def _convert(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                          "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                                         for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def chat(self, system, messages, tools=None, max_tokens=1024):
        kwargs = dict(model=self.model, messages=self._convert(system, messages), max_completion_tokens=max_tokens)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        def call():
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as e:
                if "temperature" in str(e).lower() and "temperature" in kwargs:  # reasoning models
                    kwargs.pop("temperature")
                    self.temperature = None
                    return self.client.chat.completions.create(**kwargs)
                raise

        resp = with_retries(call)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": tc.function.arguments}
            calls.append({"id": tc.id, "name": tc.function.name, "args": args})
        return AssistantTurn((msg.content or "").strip(), calls)
