"""LangGraph wiring for one patient turn.

    START -> screen -> agent --(tool calls)--> tools -> agent ...
                         |--(final text)----> guard -> END
                         |--(step budget hit)-> fallback -> guard -> END

One graph invocation = one patient message in, one reply out. State between turns is a
plain dict {messages, session, trace} held by SchedulingAgent. In production you'd swap
that for a LangGraph checkpointer keyed by conversation id. It's kept explicit here so the
eval harness can snapshot it.

`session` is the structured source of truth (who is verified, what is staged, the turn
counter). `messages` is only what the model sees. `trace` is the audit log the evaluator
reads: every tool call with its arguments and result, plus guard firings. That's the
data a transcript-only judge never sees.
"""
from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .clinic import ClinicDB
from .llm.base import LLM
from .policy import render_system_prompt
from .safety import guard_output, screen
from .tools import dumps, execute_tool, new_session, tool_specs

MAX_MODEL_STEPS_PER_TURN = 8  # loop guard: model calls per patient message
FALLBACK_REPLY = ("I'm sorry, I'm having trouble completing that right now. I've asked our front desk to "
                  "call you back. If this is urgent, please call the clinic directly.")


class AgentState(TypedDict, total=False):
    messages: list
    session: dict
    trace: list
    steps: int
    turn_start: int


def build_graph(llm: LLM, policy: dict, db: ClinicDB):
    specs = tool_specs(policy.get("tool_descriptions"))

    def screen_node(state: AgentState):
        session = dict(state["session"])
        session["turn"] += 1
        last_user = next(m for m in reversed(state["messages"]) if m["role"] == "user")
        flag = screen(last_user["content"])
        session["red_flag"] = flag  # this turn only: drives the output guard
        session["red_flag_seen"] = session.get("red_flag_seen") or flag  # sticky: drives the booking safety note
        trace = state["trace"] + [{"type": "user", "turn": session["turn"], "content": last_user["content"]}]
        if flag:
            trace.append({"type": "red_flag", "turn": session["turn"], "flag": flag})
        return {"session": session, "trace": trace, "steps": 0, "turn_start": len(state["messages"])}

    def agent_node(state: AgentState):
        system = render_system_prompt(policy, state["session"], db)
        turn = llm.chat(system, state["messages"], specs)
        msg = turn.as_message()
        trace = state["trace"] + [{"type": "assistant", "turn": state["session"]["turn"],
                                   "content": msg["content"], "tool_calls": msg["tool_calls"]}]
        return {"messages": state["messages"] + [msg], "trace": trace, "steps": state.get("steps", 0) + 1}

    def tools_node(state: AgentState):
        session, msgs, trace = state["session"], list(state["messages"]), list(state["trace"])
        for tc in state["messages"][-1]["tool_calls"]:
            result, session = execute_tool(tc["name"], tc["args"], session, db)
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": dumps(result)})
            trace.append({"type": "tool", "turn": session["turn"], "name": tc["name"], "args": tc["args"],
                          "result": result, "ok": bool(result.get("ok")),
                          "session_patient": session.get("verified_patient_id")})
        return {"messages": msgs, "session": session, "trace": trace}

    def fallback_node(state: AgentState):
        result, session = execute_tool("escalate_to_human", {"reason": "agent step budget exceeded",
                                                             "urgency": "routine"}, state["session"], db)
        msgs = list(state["messages"])
        # Close out dangling tool calls so the transcript stays valid for the provider APIs.
        for tc in msgs[-1].get("tool_calls") or []:
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                         "content": dumps({"ok": False, "error": "step_budget_exceeded"})})
        msgs.append({"role": "assistant", "content": FALLBACK_REPLY, "tool_calls": []})
        trace = state["trace"] + [{"type": "fallback", "turn": session["turn"], "ticket": result.get("ticket")}]
        return {"messages": msgs, "session": session, "trace": trace}

    def guard_node(state: AgentState):
        msgs = list(state["messages"])
        last = dict(msgs[-1])
        trace = list(state["trace"])
        if not last.get("content"):
            last["content"] = "Sorry, could you say that again?"
            trace.append({"type": "empty_reply", "turn": state["session"]["turn"]})
        earlier = " ".join(m.get("content") or "" for m in msgs[state.get("turn_start", 0):-1]
                           if m["role"] == "assistant")
        new, fired = guard_output(last["content"], state["session"].get("red_flag"), earlier)
        if fired:
            trace.append({"type": "guard_fired", "turn": state["session"]["turn"], "flag": state["session"]["red_flag"]})
            last["content"] = new
        msgs[-1] = last
        return {"messages": msgs, "trace": trace}

    def route(state: AgentState):
        last = state["messages"][-1]
        if last.get("tool_calls"):
            return "fallback" if state.get("steps", 0) >= MAX_MODEL_STEPS_PER_TURN else "tools"
        return "guard"

    g = StateGraph(AgentState)
    g.add_node("screen", screen_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("fallback", fallback_node)
    g.add_node("guard", guard_node)
    g.add_edge(START, "screen")
    g.add_edge("screen", "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", "guard": "guard", "fallback": "fallback"})
    g.add_edge("tools", "agent")
    g.add_edge("fallback", "guard")
    g.add_edge("guard", END)
    return g.compile()


class SchedulingAgent:
    """Holds conversation state across turns and runs the graph once per patient message."""

    def __init__(self, llm: LLM, policy: dict, db: ClinicDB | None = None):
        self.db = db or ClinicDB()
        self.graph = build_graph(llm, policy, self.db)
        self.state: AgentState = {"messages": [], "session": new_session(), "trace": [], "steps": 0}

    def respond(self, user_text: str) -> str:
        start = len(self.state["messages"])
        inp = {**self.state, "messages": self.state["messages"] + [{"role": "user", "content": user_text}]}
        self.state = self.graph.invoke(inp, {"recursion_limit": 4 * MAX_MODEL_STEPS_PER_TURN + 10})
        # The patient sees every piece of assistant text produced this turn, not just the last one.
        texts = [m["content"] for m in self.state["messages"][start:]
                 if m["role"] == "assistant" and m.get("content")]
        return "\n\n".join(texts)

    def transcript(self) -> list[dict]:
        return self.state["messages"]

    def dump_state(self) -> str:
        return json.dumps({"session": self.state["session"]}, indent=2)
