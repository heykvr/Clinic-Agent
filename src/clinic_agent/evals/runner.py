"""Run scenarios x trials against a policy and score them.

    python -m clinic_agent.evals.runner                       # current policy, all scenarios, 3 trials
    python -m clinic_agent.evals.runner --policy policies/v0.yaml --trials 1 --only emergency_subtle_stroke

Scoring, per trial:
  * any failed CRITICAL check  -> trial score 0 (a safety failure can't be averaged away)
  * otherwise                  -> weighted fraction of checks passed
Per scenario: mean score and critical-failure rate over k trials. The model is stochastic, so a
single run is an anecdote. k trials give a rate, and the loop's regression gate works on rates.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from ..clinic import ClinicDB
from ..graph import SchedulingAgent
from ..llm import get_llm
from ..policy import ROOT, load_policy
from .checks import global_deterministic, render_trace, run_check
from .judge import judge
from .simulator import PatientSimulator

SCENARIO_FILE = ROOT / "scenarios" / "suite.yaml"
RUNS_DIR = ROOT / "runs"


def load_scenarios(path=SCENARIO_FILE, only: list[str] | None = None, split: str | None = None) -> list[dict]:
    sc = yaml.safe_load(Path(path).read_text())["scenarios"]
    if only:
        sc = [s for s in sc if s["id"] in only]
    if split:
        sc = [s for s in sc if s["split"] == split]
    return sc


def run_trial(scenario: dict, policy: dict, trial: int, llms: dict) -> dict:
    t0 = time.time()
    db = ClinicDB(faults=scenario.get("faults") or {})
    agent = SchedulingAgent(llms["agent"](), policy, db)
    sim = PatientSimulator(scenario, llms["patient"]())
    history: list[tuple[str, str]] = []
    error = None
    try:
        msg = sim.first()
        for _ in range(scenario.get("max_turns", 10)):
            if msg is None:
                break
            history.append(("patient", msg))
            history.append(("agent", agent.respond(msg)))
            msg = sim.next(history)
    except Exception:
        error = traceback.format_exc(limit=4)

    trace = agent.state["trace"]
    ctx = {"db": db, "trace": trace, "session": agent.state["session"]}
    checks = global_deterministic(ctx)
    judge_specs = []
    for spec in scenario["checks"]:
        if spec["type"] == "judge":
            judge_specs.append(spec)
        else:
            checks.append(run_check(spec, ctx))
    judged = judge(llms["judge"](), trace, judge_specs, scenario.get("description", scenario["id"]))
    checks += judged
    # A judge that returned nothing at all (quota, outage) is an infra error, not an agent failure.
    if error is None and judged and all(c.detail.startswith("judge returned no verdict") for c in judged):
        error = f"judge infra failure: {judged[0].detail}"

    critical_fail = any(c.critical and not c.passed for c in checks) or error is not None
    total_w = sum(c.weight for c in checks)
    score = 0.0 if critical_fail else sum(c.weight for c in checks if c.passed) / total_w
    return {
        "scenario": scenario["id"], "split": scenario["split"], "trial": trial, "score": round(score, 4),
        "critical_fail": critical_fail, "error": error, "checks": [c.to_dict() for c in checks],
        "transcript": render_trace(trace), "history": history, "events": db.events,
        "seconds": round(time.time() - t0, 1),
    }


def default_llms():
    return {"agent": lambda: get_llm("agent"), "patient": lambda: get_llm("patient"),
            "judge": lambda: get_llm("judge")}


def run_suite(policy: dict, scenarios: list[dict], trials: int = 3, workers: int = 4, llms=None,
              progress=True) -> dict:
    llms = llms or default_llms()
    jobs = [(s, t) for s in scenarios for t in range(trials)]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_trial, s, policy, t, llms) for s, t in jobs]
        for i, f in enumerate(futs, 1):
            r = f.result()
            results.append(r)
            if progress:
                mark = "✗" if r["critical_fail"] else ("✓" if r["score"] >= 0.999 else "~")
                print(f"  [{i:>3}/{len(jobs)}] {mark} {r['scenario']:<26} t{r['trial']} score={r['score']:.2f}"
                      + ("  ERROR" if r["error"] else ""), flush=True)
    return summarize(results, policy)


def summarize(results: list[dict], policy: dict) -> dict:
    by: dict[str, list] = {}
    for r in results:
        by.setdefault(r["scenario"], []).append(r)
    per = {}
    for sid, all_rs in by.items():
        # Infra errors (API timeouts etc.) aren't agent behaviour: exclude them from rates but report them.
        rs = [r for r in all_rs if not r["error"]] or all_rs
        per[sid] = {
            "split": rs[0]["split"],
            "mean": round(statistics.mean(r["score"] for r in rs), 4),
            "critical_rate": round(sum(r["critical_fail"] for r in rs) / len(rs), 4),
            "pass_rate": round(sum(r["score"] >= 0.999 for r in rs) / len(rs), 4),
            "n": len(rs),
            "errors": sum(1 for r in all_rs if r["error"]),
        }

    def agg(split=None):
        xs = [v for v in per.values() if split is None or v["split"] == split]
        if not xs:
            return {"mean": None, "critical_rate": None}
        return {"mean": round(statistics.mean(x["mean"] for x in xs), 4),
                "critical_rate": round(statistics.mean(x["critical_rate"] for x in xs), 4)}

    return {"policy_version": policy["version"], "overall": agg(), "train": agg("train"),
            "holdout": agg("holdout"), "per_scenario": per, "trials": results}


def print_summary(s: dict, title: str = ""):
    print(f"\n=== {title or 'Results'} (policy v{s['policy_version']}) ===")
    print(f"{'scenario':<26}{'split':<9}{'mean':>6}{'pass':>7}{'crit':>7}")
    for sid, v in s["per_scenario"].items():
        print(f"{sid:<26}{v['split']:<9}{v['mean']:>6.2f}{v['pass_rate']:>7.2f}{v['critical_rate']:>7.2f}"
              + (f"  ({v['errors']} errors)" if v["errors"] else ""))
    for k in ("train", "holdout", "overall"):
        a = s[k]
        if a["mean"] is not None:
            print(f"{k.upper():<35}{a['mean']:>6.2f}{'':>7}{a['critical_rate']:>7.2f}")


def save(obj: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--split", choices=["train", "holdout"])
    args = ap.parse_args()
    policy = load_policy(args.policy)
    sc = load_scenarios(only=args.only, split=args.split)
    s = run_suite(policy, sc, args.trials, args.workers)
    print_summary(s)
    out = RUNS_DIR / f"eval-{time.strftime('%Y%m%d-%H%M%S')}-v{policy['version']}.json"
    save(s, out)
    print(f"\nFull results + transcripts: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
