"""Interactive chat with the scheduling agent.

    python -m clinic_agent.chat              # current policy (policies/CURRENT)
    python -m clinic_agent.chat --policy policies/v0.yaml --trace

Try: "Hi, I'm David Chen, born 2 Nov 1972. I'd like a follow-up next week, mornings."
Type /state to print session state, /quit to exit.
"""
from __future__ import annotations

import argparse
import json

from .clinic import ClinicDB
from .graph import SchedulingAgent
from .llm import get_llm
from .policy import load_policy

DIM, BOLD, CYAN, YEL, RST = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=None)
    ap.add_argument("--trace", action="store_true", help="show tool calls and guard events")
    args = ap.parse_args()

    policy = load_policy(args.policy)
    agent = SchedulingAgent(get_llm("agent"), policy, ClinicDB())
    print(f"{BOLD}Riverside Family Clinic scheduling assistant{RST} {DIM}(policy v{policy['version']}, "
          f"clinic date {agent.db.today}){RST}")
    print(f"{DIM}Test patients: David Chen 1972-11-02 · Maria Lopez 1985-03-14 · Priya Nair 1990-07-21 "
          f"(child Leo Nair 2018-05-09){RST}\n")
    while True:
        try:
            text = input(f"{CYAN}you>{RST} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/state":
            print(agent.dump_state())
            continue
        n = len(agent.state["trace"])
        reply = agent.respond(text)
        if args.trace:
            for ev in agent.state["trace"][n:]:
                if ev["type"] == "tool":
                    print(f"{DIM}  ⚙ {ev['name']}({json.dumps(ev['args'])}) -> {json.dumps(ev['result'])[:240]}{RST}")
                elif ev["type"] in ("red_flag", "guard_fired", "fallback"):
                    print(f"{YEL}  ! {ev['type']}: {ev.get('flag', '')}{RST}")
        print(f"{BOLD}agent>{RST} {reply}\n")


if __name__ == "__main__":
    main()
