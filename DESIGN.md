# Design note

**Core idea: decide what the model is *trusted* with.** What must never go wrong is enforced in code. What needs judgement lives in a versioned policy the loop may edit. The loop never touches the code or the grader.

| Layer | Owns | Changed by |
|---|---|---|
| Code (`tools.py`, `clinic.py`, `safety.py`) | identity gate, record scoping, eligibility, two-phase writes, emergency backstop | humans only |
| Policy (`policies/vN.yaml`) | persona, named rules, tool descriptions | improvement loop (gated) |

**Agent.** LangGraph turn graph `screen → agent ⇄ tools → guard`, with a step budget that falls back to a human handoff. Structured session state (verified patient, staged action, red flag) is re-rendered into the prompt each turn, so the model never has to recover "who is verified" from the transcript. "Now" is pinned, with a 14-day calendar, so "this Friday" is reproducible.

**Tools are scoped, not trusted.** No tool takes a `patient_id`: records come from whoever verified *this session*, so injection has nothing to call. Writes are two-phase (`propose_*`, then `confirm_action` only after a new patient message), so the model can't book in the turn it asked. Verification locks after 3 failures without saying which field was wrong. Reschedule is atomic. A keyword screen and output guard backstop emergencies, deliberately low-recall; `guard_not_needed` tells "model handled it" from "guard saved it".

**Evaluation.** 16 scenarios (10 train / 6 holdout): happy paths, subtle emergencies, advice bait, wrong DOB, injection, no availability, an injected slot-taken race, change of mind, relative dates, parent-for-child, billing, crisis. A simulated patient (persona or fixed script) drives each. *Deterministic checks* read ground truth: DB state, writes, tool arguments, foreign-patient leaks, write claims made before any write. An *LLM judge* covers what code can't (advice, grounding, read-back). It quotes evidence, and quotes are verified before failures reach the improver. A critical failure zeroes the trial; k trials per scenario give rates, not anecdotes. The judge is calibrated on hand-labelled near-misses before it gates anything.

**Loop.** Train failures → improver gives a root cause and ≤3 typed patches (`add_rule` / `edit_rule` / `edit_tool_description`) → a lint rejects names, dates, ids and weakened rules → the candidate re-runs on **all** scenarios, with suspected regressions re-sampled under both policies. Accept only if no scenario's critical rate rises, no mean drops more than 0.10, train rises ≥ 0.02 and holdout holds.

**Assumptions.** Identity = name + DOB; a caller acts for someone by verifying them; one active patient per session; 911/988; in-memory DB.

**Known limits (learned from the runs).**
- *Noise:* with k=3, unchanged v0 scored 1.00 then 0.50 on one scenario. Small effects sit near the noise floor, and 1 of 4 candidates was accepted.
- *Patch side effects:* "also escalate" rules made the agent over-claim ("help is on the way"; "I've notified staff" before doing it) unless paired with a grounding rule. The critical gate caught it on scenarios the patch didn't target.
- *Judge:* 24/24 on short near-misses, yet it missed a false "scheduled" claim in a long transcript (the code check caught it). An 8B judge scored 6/10 on grounding. Agent and judge are both Gemini.
- *Harness bugs look like agent failures:* a leak check firing on slot lists, tool results truncated before the judge, a simulated patient hanging up on "yes" (all in `NOTES.md`).
- Train and holdout are both mine. Next: checkpointer, EHR adapter, PHI-safe logs, more trials, real failures as scenarios.

**AI usage.** Claude drafted the first version (code, scenarios, docs) in a sandbox with no packages or models. Claude Code made it run here: Gemini integration fixes, harness bugs found by reading live transcripts, calibration, the runs and these write-ups, with every harness change logged in `NOTES.md`. Where my judgement overrode it: _[to be written by me]_
