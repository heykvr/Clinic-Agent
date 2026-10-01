"""The policy is the agent's behaviour surface that the improvement loop is allowed to change.

It's a versioned YAML document (persona, a list of named rules, tool description overrides).
It is deliberately NOT one free-form prompt string. Named rules give us:
  * reviewable diffs ("added rule urgent-not-emergency"), not a rewritten wall of text;
  * attribution (each rule records which failed checks it was added to fix);
  * rollback of a single rule.

Code-enforced guarantees (tools.py, safety.py) are NOT in the policy and can't be
changed by the loop. The loop can make the agent better, but it can't make it less safe.
"""
from __future__ import annotations

import datetime as dt
import difflib
from pathlib import Path

import yaml

from .clinic import APPOINTMENT_TYPES, PROVIDERS, EMERGENCY_NUMBER

ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / "policies"


def load_policy(path: str | Path | None = None) -> dict:
    if path is None:
        cur = (POLICY_DIR / "CURRENT").read_text().strip()
        path = POLICY_DIR / cur
    p = yaml.safe_load(Path(path).read_text())
    p.setdefault("rules", [])
    p.setdefault("tool_descriptions", {})
    p["_path"] = str(path)
    return p


def dump_policy(policy: dict) -> str:
    clean = {k: v for k, v in policy.items() if not k.startswith("_")}
    return yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=100)


def save_policy(policy: dict, make_current: bool = True) -> Path:
    path = POLICY_DIR / f"v{policy['version']}.yaml"
    path.write_text(dump_policy(policy))
    if make_current:
        (POLICY_DIR / "CURRENT").write_text(path.name + "\n")
    return path


def policy_diff(a: dict, b: dict) -> str:
    return "".join(difflib.unified_diff(dump_policy(a).splitlines(True), dump_policy(b).splitlines(True),
                                        f"v{a['version']}", f"v{b['version']}"))


def _calendar(today: dt.date, days: int = 14) -> str:
    rows = []
    for i in range(days):
        d = today + dt.timedelta(days=i)
        tag = " (today)" if i == 0 else " (tomorrow)" if i == 1 else ""
        rows.append(f"{d.strftime('%a')} {d.isoformat()}{tag}")
    return "; ".join(rows)


def render_system_prompt(policy: dict, session: dict, db) -> str:
    """Static policy + a freshly rendered block of session facts.

    The facts block is rebuilt every turn from structured state, so the model never has
    to work out from a long transcript who is verified or what is pending.
    """
    rules = "\n".join(f"{i}. [{r['id']}] {r['text'].strip()}" for i, r in enumerate(policy["rules"], 1))
    providers = "\n".join(f"- {pid}: {p['name']}, {p['specialty']}" for pid, p in PROVIDERS.items())
    types = ", ".join(f"{k} ({v['minutes']} min)" for k, v in APPOINTMENT_TYPES.items())

    pid = session.get("verified_patient_id")
    who = "nobody yet"
    if pid:
        p = db.patients[pid]
        who = f"{p['name']} (age {db.age(pid)})"
    pending = session.get("pending_action")
    facts = [
        f"Now: {db.now.strftime('%A %Y-%m-%d %H:%M')} (clinic local time).",
        f"Calendar: {_calendar(db.today)}.",
        f"Verified patient: {who}.",
        f"Staged action awaiting patient confirmation: {pending['summary'] if pending else 'none'}.",
    ]
    if session.get("red_flag"):
        facts.append(f"SAFETY ALERT: the patient's latest message matched emergency keywords "
                     f"({session['red_flag']}). Tell them to call {EMERGENCY_NUMBER} before anything else.")
    return (
        f"{policy['persona'].strip()}\n\n"
        f"## Rules\n{rules}\n\n"
        f"## Clinic reference\nProviders:\n{providers}\nAppointment types: {types}\n\n"
        f"## Session facts (authoritative, rebuilt every turn)\n" + "\n".join(f"- {f}" for f in facts)
    )
