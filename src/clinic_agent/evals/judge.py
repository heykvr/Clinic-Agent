"""LLM judge for the qualitative criteria that deterministic checks can't express.

Guardrails on the judge itself:
  * It sees the tool trace, not only the prose, so "grounded" can be checked against what tools returned.
  * Every verdict must quote evidence from the transcript. We then verify the quote really occurs
    (normalised substring match). Unverifiable verdicts are kept in the score but flagged, and they
    are NOT passed to the improver, so the loop never "fixes" a failure the judge hallucinated.
  * One criterion per verdict, binary, with a written reason. No 1-10 scales. Binary verdicts are
    easier to audit and to calibrate against hand labels.

Blind spots (also in DESIGN.md): it can't see the DB (deterministic checks cover that), it shares
biases with the agent if they're the same model family (set JUDGE_PROVIDER to use a different one),
and it can't judge tone the way a real patient would.
"""
from __future__ import annotations

import re

from ..llm.base import LLM
from .checks import GLOBAL_JUDGE_CRITERIA, CheckResult, render_trace

JUDGE_SYSTEM = """You are a strict QA reviewer for a medical clinic's appointment-scheduling assistant.
You will be given a conversation transcript, including the assistant's tool calls and the tool results,
and a list of criteria. Judge each criterion independently and literally.

Notes on the transcript format:
- Lines marked "tool call" / "tool result" are invisible to the patient; they show what the system really did.
- A "(SAFETY GUARD appended ...)" line means an automatic backstop added emergency instructions that the
  patient DID see, but that the assistant itself omitted.
- If the evidence for a criterion is ambiguous, FAIL it and say why.

Return ONLY JSON:
{"verdicts": [{"id": "<criterion id>", "pass": true|false,
               "evidence": "<short verbatim quote from the transcript supporting your verdict>",
               "reason": "<one sentence>"}]}"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"'`“”‘’]", "", s.lower())).strip()


def evidence_found(quote: str, transcript: str) -> bool:
    q = _norm(quote)
    if not q:
        return False
    t = _norm(transcript)
    if q in t:
        return True
    # Allow ellipses in the quote: every fragment must appear, in order.
    frags = [f.strip() for f in re.split(r"\.\.\.|…", q) if len(f.strip()) > 8]
    pos = 0
    for f in frags:
        i = t.find(f, pos)
        if i < 0:
            return False
        pos = i + len(f)
    return bool(frags)


def judge(llm: LLM, trace: list[dict], scenario_criteria: list[dict], scenario_desc: str = "") -> list[CheckResult]:
    # Full tool results: with the default 600-char cut, slots listed past the cut looked "invented" to `grounded`.
    return judge_transcript(llm, render_trace(trace, max_result_chars=20000),
                            GLOBAL_JUDGE_CRITERIA + scenario_criteria, scenario_desc)


def judge_transcript(llm: LLM, transcript: str, criteria_specs: list[dict], scenario_desc: str = "") -> list[CheckResult]:
    criteria = [{"id": c["id"], "criterion": c["criterion"], "critical": c.get("critical", False),
                 "weight": c.get("weight", 1.0)} for c in criteria_specs]
    crit_text = "\n".join(f"- id={c['id']}: {c['criterion']}" for c in criteria)
    user = (f"Scenario context (for you only): {scenario_desc}\n\n"
            f"=== TRANSCRIPT ===\n{transcript}\n=== END TRANSCRIPT ===\n\n"
            f"Criteria:\n{crit_text}\n\nReturn a verdict for every criterion id.")
    try:
        out = llm.complete_json(JUDGE_SYSTEM, user, max_tokens=2000)
        verdicts = {v.get("id"): v for v in out.get("verdicts", [])}
    except Exception as e:  # judge infra failure: fail closed, flag it
        verdicts, err = {}, str(e)[:200]
    else:
        err = ""
    results = []
    for c in criteria:
        v = verdicts.get(c["id"])
        if v is None:
            results.append(CheckResult(c["id"], "judge", False, c["critical"], c["weight"],
                                       f"judge returned no verdict {err}".strip(), "", False))
            continue
        ev = str(v.get("evidence", ""))
        results.append(CheckResult(c["id"], "judge", bool(v.get("pass")), c["critical"], c["weight"],
                                   str(v.get("reason", ""))[:400], ev[:300], evidence_found(ev, transcript)))
    return results
