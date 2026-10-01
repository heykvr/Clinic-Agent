"""Measure judge agreement with hand labels before trusting it as a gate.

    python -m clinic_agent.evals.calibrate_judge [--repeats 3]

If the judge disagrees with the gold labels on a criterion, tighten that criterion's wording (or move the
check into deterministic code) BEFORE running the loop. A loop that optimises against a miscalibrated
judge just learns to please the judge.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import yaml

from ..llm import get_llm
from ..policy import ROOT
from .judge import judge_transcript


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    gold = yaml.safe_load((ROOT / "scenarios" / "judge_gold.yaml").read_text())
    llm = get_llm("judge")
    agree, total = defaultdict(int), defaultdict(int)
    misses = []
    for case in gold["cases"]:
        specs = [{"id": k, "criterion": gold["criteria"][k]} for k in case["labels"]]
        for _ in range(args.repeats):
            for r in judge_transcript(llm, case["transcript"], specs):
                want = case["labels"][r.id]
                total[r.id] += 1
                if r.passed == want:
                    agree[r.id] += 1
                else:
                    misses.append((case["id"], r.id, want, r.passed, r.detail))
    print(f"Judge model: {llm.model}\n")
    print(f"{'criterion':<22}{'agreement':>10}")
    for k in total:
        print(f"{k:<22}{agree[k]}/{total[k]:<8}")
    print(f"{'ALL':<22}{sum(agree.values())}/{sum(total.values())}")
    for m in misses:
        print(f"\n✗ {m[0]} / {m[1]}: gold={m[2]} judge={m[3]}\n  judge reason: {m[4]}")


if __name__ == "__main__":
    main()
