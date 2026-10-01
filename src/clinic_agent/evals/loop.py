"""The closed improvement loop.

    python -m clinic_agent.evals.loop                    # from policies/CURRENT, 2 iterations, 3 trials
    python -m clinic_agent.evals.loop --policy policies/v0.yaml --iterations 1 --trials 3
    python -m clinic_agent.evals.loop --baseline runs/eval-...json   # reuse a saved baseline run

Each iteration:
  1. EVAL     baseline policy on ALL scenarios x k trials
  2. COLLECT  failing checks from the TRAIN split only (holdout stays unseen)
  3. PROPOSE  structured patch (improver LLM), then overfitting lint
  4. APPLY    -> candidate policy vN+1
  5. RE-EVAL  candidate on ALL scenarios x k trials
  6. CONFIRM  suspected regressions get re-run (both policies, k more trials, pooled) before we judge them
  7. GATE     accept only if:
                - no scenario's critical-failure rate goes up              (safety is never traded)
                - no scenario's mean score drops by more than --tolerance  (no regressions)
                - the train mean improves by at least --min-gain           (the fix actually works)
                - the holdout mean doesn't drop by more than --tolerance   (it generalises)
     Accepted -> policies/vN+1.yaml becomes CURRENT. Rejected -> logged with reasons, and the
     rejection reasons go back to the improver on the next attempt.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..llm import get_llm
from ..policy import ROOT, load_policy, policy_diff, save_policy
from .improver import apply_patches, collect_failures, propose
from .runner import RUNS_DIR, load_scenarios, print_summary, run_suite, save, summarize

B, G, R, Y, D, X = "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def banner(s):
    print(f"\n{B}{'=' * 78}\n{s}\n{'=' * 78}{X}")


def gate(base: dict, cand: dict, tolerance: float, min_gain: float) -> dict:
    regressions, improvements = [], []
    for sid, b in base["per_scenario"].items():
        c = cand["per_scenario"].get(sid)
        if c is None:
            continue
        if c["critical_rate"] > b["critical_rate"] + 1e-9:
            regressions.append({"scenario": sid, "kind": "critical",
                                "before": b["critical_rate"], "after": c["critical_rate"]})
        elif c["mean"] < b["mean"] - tolerance:
            regressions.append({"scenario": sid, "kind": "score", "before": b["mean"], "after": c["mean"]})
        if c["mean"] > b["mean"] + 1e-9:
            improvements.append({"scenario": sid, "before": b["mean"], "after": c["mean"]})
    reasons = []
    if regressions:
        reasons.append("regressions: " + ", ".join(f"{r['scenario']}({r['kind']} {r['before']:.2f}->{r['after']:.2f})"
                                                     for r in regressions))
    gain = cand["train"]["mean"] - base["train"]["mean"]
    if gain < min_gain:
        reasons.append(f"train mean gain {gain:+.3f} < required {min_gain:+.3f}")
    if base["holdout"]["mean"] is not None and cand["holdout"]["mean"] < base["holdout"]["mean"] - tolerance:
        reasons.append(f"holdout mean dropped {base['holdout']['mean']:.2f}->{cand['holdout']['mean']:.2f}")
    return {"accepted": not reasons, "reasons": reasons, "regressions": regressions,
            "improvements": improvements, "train_gain": round(gain, 4)}


def compare_table(base: dict, cand: dict) -> str:
    rows = [f"| scenario | split | before | after | Δ | crit before | crit after |",
            "|---|---|---|---|---|---|---|"]
    for sid, b in base["per_scenario"].items():
        c = cand["per_scenario"].get(sid, b)
        d = c["mean"] - b["mean"]
        rows.append(f"| {sid} | {b['split']} | {b['mean']:.2f} | {c['mean']:.2f} | {d:+.2f} | "
                    f"{b['critical_rate']:.2f} | {c['critical_rate']:.2f} |")
    for k in ("train", "holdout", "overall"):
        if base[k]["mean"] is not None:
            rows.append(f"| **{k}** | | **{base[k]['mean']:.2f}** | **{cand[k]['mean']:.2f}** | "
                        f"**{cand[k]['mean'] - base[k]['mean']:+.2f}** | {base[k]['critical_rate']:.2f} | "
                        f"{cand[k]['critical_rate']:.2f} |")
    return "\n".join(rows)


def print_compare(base, cand):
    print(f"\n{'scenario':<26}{'split':<9}{'before':>7}{'after':>7}{'Δ':>7}{'crit b/a':>11}")
    for sid, b in base["per_scenario"].items():
        c = cand["per_scenario"].get(sid, b)
        d = c["mean"] - b["mean"]
        col = G if d > 0.001 else R if d < -0.001 else ""
        print(f"{sid:<26}{b['split']:<9}{b['mean']:>7.2f}{c['mean']:>7.2f}{col}{d:>+7.2f}{X}"
              f"{b['critical_rate']:>6.2f}/{c['critical_rate']:.2f}")
    for k in ("train", "holdout", "overall"):
        if base[k]["mean"] is not None:
            d = cand[k]["mean"] - base[k]["mean"]
            print(f"{B}{k.upper():<35}{base[k]['mean']:>7.2f}{cand[k]['mean']:>7.2f}{d:>+7.2f}{X}")


def confirm_regressions(base, cand, base_policy, cand_policy, scenarios, trials, workers, tolerance, min_gain):
    """Noise control: before rejecting on a regression, re-sample the suspect scenarios under BOTH
    policies and pool the trials. Rejecting a good patch is cheap (we try again); accepting a real
    regression is not. So we re-check, but the final rule stays strict."""
    g = gate(base, cand, tolerance, min_gain)
    suspects = sorted({r["scenario"] for r in g["regressions"]})
    if not suspects:
        return base, cand, g
    print(f"\n{Y}Suspected regressions in {suspects}: re-running {trials} more trials under both policies to "
          f"separate signal from sampling noise...{X}")
    sc = [s for s in scenarios if s["id"] in suspects]
    extra_b = run_suite(base_policy, sc, trials, workers, progress=False)["trials"]
    extra_c = run_suite(cand_policy, sc, trials, workers, progress=False)["trials"]
    base2 = summarize(base["trials"] + [dict(t, trial=t["trial"] + 100) for t in extra_b], base_policy)
    cand2 = summarize(cand["trials"] + [dict(t, trial=t["trial"] + 100) for t in extra_c], cand_policy)
    g2 = gate(base2, cand2, tolerance, min_gain)
    g2["confirmed_with_extra_trials"] = suspects
    return base2, cand2, g2


def write_report(path: Path, ctx: dict):
    L = [f"# Improvement loop report\n", f"- started: {ctx['started']}",
         f"- agent model: `{ctx['models']['agent']}` · judge: `{ctx['models']['judge']}` · "
         f"improver: `{ctx['models']['improver']}`",
         f"- trials per scenario: {ctx['trials']} · tolerance: {ctx['tolerance']} · min gain: {ctx['min_gain']}",
         f"- start policy: v{ctx['start_version']} → final policy: v{ctx['final_version']}\n",
         "## Before → after (start policy vs final accepted policy)\n", compare_table(ctx["first"], ctx["last"]), ""]
    for it in ctx["iterations"]:
        L.append(f"\n## Iteration {it['iteration']}: {'✅ ACCEPTED' if it['decision']['accepted'] else '❌ REJECTED'}\n")
        L.append("### Failures fed to the improver (train split)\n")
        for f in it["proposal"]["failures"]:
            L.append(f"- `{f['scenario']}/{f['check']}` {'**critical**' if f['critical'] else ''} "
                     f"failed {f['fails']}/{f['trials']}: {f['detail'][:200]}")
        L.append("\n### Diagnosis\n")
        for d in it["proposal"]["diagnosis"]:
            L.append(f"- `{d.get('failure')}`: {d.get('root_cause')}")
        L.append("\n### Patch\n")
        for p in it["proposal"]["patches"]:
            L.append(f"- **{p['op']}** `{p.get('id') or p.get('tool')}`: {p['text']}\n  - addresses: "
                     f"{p.get('addresses')} · rationale: {p.get('rationale', '')}")
        for p in it["proposal"]["rejected_patches"]:
            L.append(f"- ~~{p.get('op')} {p.get('id') or p.get('tool')}~~ rejected by lint: {p['lint']}")
        L.append("\n### Candidate vs baseline\n")
        L.append(compare_table(it["base"], it["cand"]))
        L.append("\n### Gate\n")
        L.append("- " + ("accepted" if it["decision"]["accepted"] else "rejected: " + "; ".join(it["decision"]["reasons"])))
        if it["decision"].get("confirmed_with_extra_trials"):
            L.append(f"- regressions re-checked with extra trials on: {it['decision']['confirmed_with_extra_trials']}")
        L.append(f"\n### Policy diff\n```diff\n{it['diff']}```")
    path.write_text("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy")
    ap.add_argument("--baseline", help="reuse a saved eval JSON for the starting policy (saves a run)")
    ap.add_argument("--iterations", type=int, default=2)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tolerance", type=float, default=0.10)
    ap.add_argument("--min-gain", type=float, default=0.02)
    ap.add_argument("--only", nargs="*", help="restrict to these scenario ids (for quick demos)")
    ap.add_argument("--no-promote", action="store_true", help="don't move policies/CURRENT")
    args = ap.parse_args()

    run_dir = RUNS_DIR / f"loop-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    scenarios = load_scenarios(only=args.only)
    policy = load_policy(args.policy)
    improver = get_llm("improver")
    models = {r: get_llm(r).model for r in ("agent", "judge", "improver")}
    print(f"{D}models: {models} · run dir: {run_dir.relative_to(ROOT)}{X}")

    banner(f"STEP 1 · BASELINE EVAL  policy v{policy['version']}  ({len(scenarios)} scenarios × {args.trials} trials)")
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text())
        print(f"(reusing {args.baseline})")
    else:
        base = run_suite(policy, scenarios, args.trials, args.workers)
    save(base, run_dir / f"eval-v{policy['version']}.json")
    print_summary(base, "Baseline")

    ctx = {"started": time.strftime("%Y-%m-%d %H:%M"), "models": models, "trials": args.trials,
           "tolerance": args.tolerance, "min_gain": args.min_gain, "start_version": policy["version"],
           "first": base, "iterations": []}
    feedback = ""
    for it in range(1, args.iterations + 1):
        banner(f"STEP 2 · ITERATION {it}: collect train failures → propose patch")
        if not collect_failures(base, "train"):
            print(f"{G}No train failures left. Stopping.{X}")
            break
        proposal = propose(improver, policy, base, scenarios, feedback)
        for f in proposal["failures"]:
            print(f"  {R if f['critical'] else Y}✗{X} {f['scenario']}/{f['check']}  failed {f['fails']}/{f['trials']}"
                  f"{'  [CRITICAL]' if f['critical'] else ''}")
        print(f"\n{B}Diagnosis{X}")
        for d in proposal["diagnosis"]:
            print(f"  • {d.get('failure')}: {d.get('root_cause')}")
        print(f"\n{B}Proposed patch{X}")
        for p in proposal["patches"]:
            print(f"  {G}+ {p['op']} {p.get('id') or p.get('tool')}{X}: {p['text']}")
        for p in proposal["rejected_patches"]:
            print(f"  {R}- rejected {p.get('op')} {p.get('id') or p.get('tool')}: {p['lint']}{X}")
        if not proposal["patches"]:
            feedback = "All patches were rejected by lint: " + "; ".join(p["lint"] for p in proposal["rejected_patches"])
            print(f"{Y}No valid patches; retrying with lint feedback next iteration.{X}")
            ctx["iterations"].append({"iteration": it, "proposal": proposal, "base": base, "cand": base,
                                      "decision": {"accepted": False, "reasons": ["no valid patches"]}, "diff": ""})
            continue

        cand_policy = apply_patches(policy, proposal["patches"], it, proposal["diagnosis"])
        diff = policy_diff(policy, cand_policy)
        print(f"\n{D}{diff}{X}")

        banner(f"STEP 3 · RE-EVAL candidate v{cand_policy['version']} on ALL scenarios (train + holdout)")
        cand = run_suite(cand_policy, scenarios, args.trials, args.workers)
        base_c, cand_c, decision = confirm_regressions(base, cand, policy, cand_policy, scenarios, args.trials,
                                                       args.workers, args.tolerance, args.min_gain)
        save(cand_c, run_dir / f"eval-v{cand_policy['version']}-iter{it}.json")

        banner(f"STEP 4 · GATE  v{policy['version']} → v{cand_policy['version']}")
        print_compare(base_c, cand_c)
        it_rec = {"iteration": it, "proposal": proposal, "base": base_c, "cand": cand_c,
                  "decision": decision, "diff": diff}
        ctx["iterations"].append(it_rec)
        if decision["accepted"]:
            path = save_policy(cand_policy, make_current=not args.no_promote)
            print(f"\n{G}{B}ACCEPTED{X} → {path.relative_to(ROOT)}"
                  f"{'' if args.no_promote else ' (now policies/CURRENT)'}")
            policy, base, feedback = load_policy(path), cand_c, ""
        else:
            rej = run_dir / f"rejected-v{cand_policy['version']}-iter{it}.yaml"
            from ..policy import dump_policy
            rej.write_text(dump_policy(cand_policy))
            print(f"\n{R}{B}REJECTED{X}: " + "; ".join(decision["reasons"]))
            feedback = ("Your previous patch was REJECTED by the regression gate: " + "; ".join(decision["reasons"])
                        + f". Rejected patch: {json.dumps(proposal['patches'])}. Propose a different fix.")
            base = base_c  # keep the (possibly re-sampled) baseline

    ctx["last"], ctx["final_version"] = base, policy["version"]
    report = run_dir / "report.md"
    write_report(report, ctx)
    banner("DONE")
    print_compare(ctx["first"], ctx["last"])
    print(f"\nReport: {report.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
