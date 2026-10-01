from clinic_agent.clinic import ClinicDB
from clinic_agent.graph import MAX_MODEL_STEPS_PER_TURN, SchedulingAgent
from clinic_agent.llm import ScriptedLLM
from clinic_agent.llm.anthropic_llm import AnthropicLLM
from clinic_agent.llm.base import AssistantTurn as T, extract_json
from clinic_agent.llm.openai_llm import OpenAILLM
from clinic_agent.policy import load_policy
from clinic_agent.safety import screen


def test_keyword_screen_catches_explicit_and_misses_subtle():
    assert screen("I have crushing chest pain") == "chest_pain"
    assert screen("I've been thinking about ending my life") == "suicide"
    # Documented blind spot: stroke signs without the keyword. The model and the eval must cover these.
    assert screen("my face feels numb and my words are slurred") is None


def test_guard_appends_emergency_line_when_model_forgets():
    agent = SchedulingAgent(ScriptedLLM([T("Sure! What's your date of birth?")]), load_policy(), ClinicDB())
    reply = agent.respond("crushing chest pain, can I come in today?")
    assert "911" in reply
    assert any(e["type"] == "guard_fired" for e in agent.state["trace"])


def test_guard_silent_when_model_handles_it():
    agent = SchedulingAgent(ScriptedLLM([T("Please call 911 right now.")]), load_policy(), ClinicDB())
    agent.respond("crushing chest pain")
    assert not any(e["type"] == "guard_fired" for e in agent.state["trace"])


def test_step_budget_fallback_escalates():
    loop = T("", [{"id": "x", "name": "get_clinic_info", "args": {"topic": "hours"}}])
    agent = SchedulingAgent(ScriptedLLM(lambda *a: loop), load_policy(), ClinicDB())
    reply = agent.respond("hi")
    assert "front desk" in reply
    assert agent.db.escalations
    assert sum(1 for e in agent.state["trace"] if e["type"] == "assistant") == MAX_MODEL_STEPS_PER_TURN


def test_session_facts_rendered_each_turn():
    llm = ScriptedLLM([T("", [{"id": "1", "name": "verify_patient",
                               "args": {"full_name": "David Chen", "date_of_birth": "1972-11-02"}}]),
                       T("Thanks David."), T("ok")])
    agent = SchedulingAgent(llm, load_policy(), ClinicDB())
    agent.respond("David Chen 1972-11-02")
    agent.respond("hello")
    assert "Verified patient: David Chen" in llm.calls[-1]["system"]


def test_provider_message_conversion():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "args": {}},
                                                               {"id": "b", "name": "y", "args": {"k": 1}}]},
            {"role": "tool", "tool_call_id": "a", "name": "x", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "name": "y", "content": "{}"}]
    a = AnthropicLLM._convert(msgs)
    assert [m["role"] for m in a] == ["user", "assistant", "user"] and len(a[2]["content"]) == 2
    o = OpenAILLM._convert("sys", msgs)
    assert [m["role"] for m in o] == ["system", "user", "assistant", "tool", "tool"]


def test_extract_json():
    assert extract_json('noise ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('Sure: {"a": {"b": 2}} done') == {"a": {"b": 2}}
