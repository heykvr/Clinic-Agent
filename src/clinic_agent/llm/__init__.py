"""Model factory. Each role can use a different provider/model.

Env vars:
  LLM_PROVIDER            anthropic | openai          (default: anthropic)
  <ROLE>_PROVIDER         per-role override, e.g. JUDGE_PROVIDER=openai
  <ROLE>_MODEL            per-role model override
  <ROLE>_TEMPERATURE      per-role temperature (default 0 for agent/judge/improver, 0.7 for patient)
Roles: AGENT, PATIENT, JUDGE, IMPROVER.

Using a different provider for the JUDGE than for the AGENT is supported on purpose. A judge
from the same model family shares the agent's blind spots.
"""
from __future__ import annotations

import os

from .base import LLM, AssistantTurn, extract_json  # noqa: F401

DEFAULT_MODELS = {
    "anthropic": {"AGENT": "claude-haiku-4-5-20251001", "PATIENT": "claude-haiku-4-5-20251001",
                  "JUDGE": "claude-sonnet-5-5", "IMPROVER": "claude-sonnet-5-5"},
    "openai": {"AGENT": "gpt-4.1-mini", "PATIENT": "gpt-4.1-mini",
               "JUDGE": "gpt-4.1", "IMPROVER": "gpt-4.1"},
}
DEFAULT_TEMPS = {"AGENT": 0.0, "PATIENT": 0.7, "JUDGE": 0.0, "IMPROVER": 0.2}


def _load_dotenv():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_llm(role: str) -> LLM:
    _load_dotenv()
    role = role.upper()
    provider = os.environ.get(f"{role}_PROVIDER") or os.environ.get("LLM_PROVIDER", "anthropic")
    model = os.environ.get(f"{role}_MODEL") or DEFAULT_MODELS[provider][role]
    t = os.environ.get(f"{role}_TEMPERATURE")
    temp = DEFAULT_TEMPS[role] if t is None else (None if t.lower() == "none" else float(t))
    if provider == "anthropic":
        from .anthropic_llm import AnthropicLLM
        return AnthropicLLM(model, temp)
    if provider == "openai":
        from .openai_llm import OpenAILLM
        return OpenAILLM(model, temp)
    raise ValueError(f"Unknown provider {provider!r}")


class ScriptedLLM(LLM):
    """Test double: replays a fixed list of turns, or calls a function(system, messages, tools)."""
    model = "scripted"

    def __init__(self, script):
        self.script = list(script) if not callable(script) else script
        self.calls: list[dict] = []

    def chat(self, system, messages, tools=None, max_tokens=1024):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if callable(self.script):
            out = self.script(system, messages, tools)
        else:
            out = self.script.pop(0)
        if isinstance(out, str):
            return AssistantTurn(out, [])
        return out
