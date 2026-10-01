"""Provider-neutral chat interface.

Internal message format (used by the graph, the eval traces and both adapters):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id","name","args"}]

    def as_message(self) -> dict:
        return {"role": "assistant", "content": self.content, "tool_calls": self.tool_calls}


class LLM:
    model: str = "?"

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 1024) -> AssistantTurn:
        raise NotImplementedError

    # Convenience for the simulator / judge / improver: ask for JSON, parse robustly, retry once.
    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict:
        msgs = [{"role": "user", "content": user}]
        last = ""
        for _ in range(2):
            last = self.chat(system, msgs, None, max_tokens).content
            obj = extract_json(last)
            if obj is not None:
                return obj
            msgs = msgs + [{"role": "assistant", "content": last, "tool_calls": []},
                           {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."}]
        raise ValueError(f"Model did not return JSON: {last[:300]}")

    def complete_text(self, system: str, user: str, max_tokens: int = 512) -> str:
        return self.chat(system, [{"role": "user", "content": user}], None, max_tokens).content


def extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def with_retries(fn, attempts: int = 6, base_delay: float = 2.0, max_delay: float = 65.0):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # rate limits / transient 5xx; surface everything else on final attempt
            name = type(e).__name__
            transient = any(k in name for k in ("RateLimit", "APIConnection", "InternalServer", "Overloaded",
                                                 "Timeout", "ServiceUnavailable"))
            if not transient or i == attempts - 1:
                raise
            # Per-minute quotas (e.g. Gemini free tier) say how long to wait; honour that instead of guessing.
            hint = re.search(r"retry in ([\d.]+)s", str(e))
            delay = float(hint.group(1)) + 1 if hint else base_delay * (2 ** i)
            time.sleep(min(delay, max_delay))
