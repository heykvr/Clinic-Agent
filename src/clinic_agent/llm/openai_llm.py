from __future__ import annotations

import json
import os

from .base import LLM, AssistantTurn, with_retries


class OpenAILLM(LLM):
    def __init__(self, model: str, temperature: float | None = None, role: str | None = None):
        import openai  # lazy
        # Per-role endpoint overrides let e.g. the agent run on a local Ollama server and the judge on a hosted API.
        env = lambda k: (role and os.environ.get(f"{role}_{k}")) or os.environ.get(f"OPENAI_{k}")  # noqa: E731
        self.client = openai.OpenAI(api_key=env("API_KEY") or "unused", base_url=env("BASE_URL") or None)
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
                                          "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                                          **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {})}
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
            call_ = {"id": tc.id, "name": tc.function.name, "args": args}
            # Gemini's OpenAI-compatible endpoint returns a thought_signature here and rejects the next
            # request (400) unless it is echoed back on the same tool call.
            extra = (tc.model_extra or {}).get("extra_content")
            if extra:
                call_["extra_content"] = extra
            calls.append(call_)
        return AssistantTurn((msg.content or "").strip(), calls)
