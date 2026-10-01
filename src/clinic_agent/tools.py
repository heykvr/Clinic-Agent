"""Tools exposed to the model, and the authorization layer around them.

Design rules (enforced in code, not in the prompt):
  1. Scope by session, not by argument. No tool takes a patient_id. Record access
     always uses the patient verified in THIS session, so "show me John Smith's
     appointments" or a prompt injection cannot reach another record.
  2. Writes are two-phase. propose_* only stage an action. confirm_action carries
     it out, and only if the patient has sent a message since the proposal. The
     model cannot propose and confirm in the same turn, so it cannot book
     without a patient reply, whatever the prompt says.
  3. Verification is rate-limited (3 failed attempts locks it for the session),
     and failures never say WHICH field was wrong.
  4. Errors are returned to the model as data, so it can recover. A tool never
     raises into the conversation.
"""
from __future__ import annotations

import datetime as dt
import json

from .clinic import APPOINTMENT_TYPES, CLINIC_INFO, PROVIDERS, ClinicDB

MAX_VERIFY_ATTEMPTS = 3

BASE_TOOL_SPECS = [
    {
        "name": "verify_patient",
        "description": "Verify the caller's patient record using full name and date of birth. Required before "
                       "any search, lookup, booking, change or cancellation. Verifying a different person "
                       "switches the active patient.",
        "parameters": {"type": "object", "properties": {
            "full_name": {"type": "string"},
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["full_name", "date_of_birth"]},
    },
    {
        "name": "search_availability",
        "description": "Find open appointment start times for the verified patient. Results are already "
                       "filtered to providers the patient is eligible to see.",
        "parameters": {"type": "object", "properties": {
            "appointment_type": {"type": "string", "enum": list(APPOINTMENT_TYPES)},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD (inclusive)"},
            "provider_id": {"type": "string", "enum": list(PROVIDERS)},
            "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]}},
            "required": ["appointment_type", "date_from", "date_to"]},
    },
    {
        "name": "list_my_appointments",
        "description": "List the verified patient's upcoming appointments.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_booking",
        "description": "Stage a booking for the verified patient. This does NOT book. Read the returned "
                       "summary to the patient and ask for an explicit yes, then call confirm_action.",
        "parameters": {"type": "object", "properties": {
            "slot_id": {"type": "string", "description": "A slot_id returned by search_availability"},
            "appointment_type": {"type": "string", "enum": list(APPOINTMENT_TYPES)},
            "reason": {"type": "string", "description": "Short reason for visit in the patient's words"}},
            "required": ["slot_id", "appointment_type", "reason"]},
    },
    {
        "name": "propose_cancellation",
        "description": "Stage cancelling one of the verified patient's appointments. Does NOT cancel until "
                       "confirm_action.",
        "parameters": {"type": "object", "properties": {"appointment_id": {"type": "string"}},
                       "required": ["appointment_id"]},
    },
    {
        "name": "propose_reschedule",
        "description": "Stage moving one of the verified patient's appointments to a new slot from "
                       "search_availability. Does NOT change anything until confirm_action.",
        "parameters": {"type": "object", "properties": {
            "appointment_id": {"type": "string"}, "new_slot_id": {"type": "string"}},
            "required": ["appointment_id", "new_slot_id"]},
    },
    {
        "name": "confirm_action",
        "description": "Carry out the staged action. Only call this after the patient has explicitly agreed "
                       "to the exact details you read back. It is rejected if the patient has not replied "
                       "since the proposal.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_clinic_info",
        "description": "Look up clinic facts: hours, address, policies, phone lines. Use this rather than "
                       "answering from memory.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "enum": list(CLINIC_INFO)}}, "required": ["topic"]},
    },
    {
        "name": "escalate_to_human",
        "description": "Hand off to clinic staff. urgency=emergency alerts staff immediately (still tell the "
                       "patient to call 911). urgency=urgent means a nurse calls back today. routine means "
                       "the front desk calls back within one business day.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"},
            "urgency": {"type": "string", "enum": ["routine", "urgent", "emergency"]}},
            "required": ["reason", "urgency"]},
    },
]

WRITE_PROPOSALS = {"propose_booking", "propose_cancellation", "propose_reschedule"}
NEEDS_VERIFIED = {"search_availability", "list_my_appointments", *WRITE_PROPOSALS, "confirm_action"}


def tool_specs(overrides: dict | None = None) -> list[dict]:
    """Tool descriptions are part of the policy surface: the improver may refine them."""
    overrides = overrides or {}
    out = []
    for spec in BASE_TOOL_SPECS:
        s = dict(spec)
        if spec["name"] in overrides:
            s["description"] = overrides[spec["name"]]
        out.append(s)
    return out


def new_session() -> dict:
    return {"verified_patient_id": None, "failed_verifications": 0, "pending_action": None,
            "turn": 0, "red_flag": None, "red_flag_seen": None}


def _date(s):
    return dt.date.fromisoformat(str(s)[:10])


def execute_tool(name: str, args: dict, session: dict, db: ClinicDB) -> tuple[dict, dict]:
    """Returns (result_payload, updated_session). Never raises."""
    session = dict(session)
    try:
        if name in NEEDS_VERIFIED and not session.get("verified_patient_id"):
            return {"ok": False, "error": "not_verified",
                    "detail": "Verify the patient's full name and date of birth first."}, session
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"ok": False, "error": "unknown_tool"}, session
        return handler(args or {}, session, db)
    except (ValueError, KeyError, TypeError) as e:
        return {"ok": False, "error": "bad_arguments", "detail": str(e)[:200]}, session


def _verify(args, session, db):
    if session["failed_verifications"] >= MAX_VERIFY_ATTEMPTS:
        return {"ok": False, "error": "locked",
                "detail": "Too many failed attempts. Offer to have the front desk call the patient back."}, session
    try:
        dob = _date(args["date_of_birth"]).isoformat()
    except ValueError:
        return {"ok": False, "error": "bad_date", "detail": "Date of birth must be YYYY-MM-DD."}, session
    matches = db.find_patient(args.get("full_name", ""), dob)
    if len(matches) != 1:
        session["failed_verifications"] += 1
        left = MAX_VERIFY_ATTEMPTS - session["failed_verifications"]
        # Deliberately vague: do not reveal whether the name or the DOB was wrong.
        return {"ok": False, "error": "no_match", "attempts_left": left,
                "detail": "No record matches that name and date of birth."}, session
    pid = matches[0]
    if session.get("verified_patient_id") != pid:
        session["pending_action"] = None  # never carry a staged action across patients
    session["verified_patient_id"] = pid
    p = db.patients[pid]
    return {"ok": True, "patient": {"first_name": p["name"].split()[0], "age": db.age(pid)}}, session


def _search(args, session, db):
    pid = session["verified_patient_id"]
    d0, d1 = _date(args["date_from"]), _date(args["date_to"])
    if d1 < d0:
        return {"ok": False, "error": "bad_range"}, session
    if args.get("provider_id"):
        why = db.eligible(pid, args["provider_id"], args["appointment_type"])
        if why:
            return {"ok": False, "error": "not_eligible", "detail": why}, session
    slots = db.search(pid, args["appointment_type"], d0, d1, args.get("provider_id"),
                      args.get("part_of_day") or "any")
    res = {"ok": True, "today": db.today.isoformat(), "slots": [db.describe_slot(s) for s in slots]}
    if not slots:
        last = db.today + dt.timedelta(days=27)
        nxt = db.search(pid, args["appointment_type"], d1 + dt.timedelta(days=1), last,
                        args.get("provider_id"), args.get("part_of_day") or "any", limit=1)
        res["next_available_after_range"] = db.describe_slot(nxt[0]) if nxt else None
        res["schedule_open_until"] = last.isoformat()
    return res, session


def _list(args, session, db):
    appts = db.list_for(session["verified_patient_id"])
    return {"ok": True, "appointments": [db.describe_appt(a) for a in appts]}, session


def _stage(session, op, args, summary, extra=None):
    session["pending_action"] = {"op": op, "args": args, "summary": summary, "created_turn": session["turn"]}
    res = {"ok": True, "status": "PENDING_PATIENT_CONFIRMATION", "summary": summary,
           "instruction": "Nothing has changed yet. Read the summary to the patient and ask them to confirm."}
    if session.get("red_flag_seen"):
        res["safety_note"] = ("Safety screen flagged possible emergency symptoms in this conversation. "
                              "Make sure the patient has been told to call 911 before scheduling anything.")
    if extra:
        res.update(extra)
    return res, session


def _propose_booking(args, session, db):
    pid = session["verified_patient_id"]
    parsed = db.parse_slot(args["slot_id"])
    if not parsed or parsed[0] not in PROVIDERS:
        return {"ok": False, "error": "unknown_slot", "detail": "Use a slot_id from search_availability."}, session
    if args["appointment_type"] not in APPOINTMENT_TYPES:
        return {"ok": False, "error": "unknown_type"}, session
    why = db.eligible(pid, parsed[0], args["appointment_type"])
    if why:
        return {"ok": False, "error": "not_eligible", "detail": why}, session
    cov = db._covered_slots(parsed[0], parsed[1], parsed[2], args["appointment_type"])
    if not cov or any(s in db.taken for s in cov):
        return {"ok": False, "error": "slot_unavailable", "detail": "Not open. Search again."}, session
    s = db.describe_slot(args["slot_id"])
    label = APPOINTMENT_TYPES[args["appointment_type"]]["label"]
    summary = f"{label} with {s['provider']} on {s['weekday']} {s['date']} at {s['time']} for {db.patients[pid]['name']}"
    return _stage(session, "book", dict(args), summary)


def _propose_cancel(args, session, db):
    pid = session["verified_patient_id"]
    a = db.appointments.get(args["appointment_id"])
    if not a or a["patient_id"] != pid or a["status"] != "booked":
        return {"ok": False, "error": "not_found", "detail": "No such upcoming appointment for this patient."}, session
    d = db.describe_appt(a)
    summary = f"Cancel {APPOINTMENT_TYPES[a['type']]['label']} with {d['provider']} on {d['weekday']} {d['date']} at {d['time']}"
    start = dt.datetime.combine(_date(a["date"]), dt.time.fromisoformat(a["time"]))
    extra = {"policy_note": "Inside 24h: a $25 late-cancellation fee may apply."} if start - db.now < dt.timedelta(hours=24) else None
    return _stage(session, "cancel", dict(args), summary, extra)


def _propose_reschedule(args, session, db):
    pid = session["verified_patient_id"]
    a = db.appointments.get(args["appointment_id"])
    if not a or a["patient_id"] != pid or a["status"] != "booked":
        return {"ok": False, "error": "not_found", "detail": "No such upcoming appointment for this patient."}, session
    parsed = db.parse_slot(args["new_slot_id"])
    if not parsed or parsed[0] not in PROVIDERS:
        return {"ok": False, "error": "unknown_slot"}, session
    why = db.eligible(pid, parsed[0], a["type"])
    if why:
        return {"ok": False, "error": "not_eligible", "detail": why}, session
    cov = db._covered_slots(parsed[0], parsed[1], parsed[2], a["type"])
    if not cov or any(s in db.taken for s in cov):
        return {"ok": False, "error": "slot_unavailable", "detail": "Not open. Search again."}, session
    old, new = db.describe_appt(a), db.describe_slot(args["new_slot_id"])
    summary = (f"Move {APPOINTMENT_TYPES[a['type']]['label']} from {old['weekday']} {old['date']} {old['time']} "
               f"to {new['weekday']} {new['date']} at {new['time']} with {new['provider']}")
    return _stage(session, "reschedule", dict(args), summary)


def _confirm(args, session, db):
    pa = session.get("pending_action")
    if not pa:
        return {"ok": False, "error": "nothing_pending", "detail": "Propose an action first."}, session
    if session["turn"] <= pa["created_turn"]:
        return {"ok": False, "error": "patient_has_not_confirmed",
                "detail": "The patient has not replied since you proposed this. Read the summary and wait for "
                          "their explicit yes."}, session
    pid, a = session["verified_patient_id"], pa["args"]
    if pa["op"] == "book":
        res = db.book(pid, a["slot_id"], a["appointment_type"], a.get("reason", ""))
    elif pa["op"] == "cancel":
        res = db.cancel(pid, a["appointment_id"])
    else:
        res = db.reschedule(pid, a["appointment_id"], a["new_slot_id"])
    session["pending_action"] = None  # consumed whether it succeeded or not; re-propose on failure
    return res, session


def _info(args, session, db):
    t = args.get("topic")
    if t not in CLINIC_INFO:
        return {"ok": False, "error": "unknown_topic", "available": list(CLINIC_INFO)}, session
    return {"ok": True, "topic": t, "info": CLINIC_INFO[t]}, session


def _escalate(args, session, db):
    urgency = args.get("urgency", "routine")
    if urgency not in ("routine", "urgent", "emergency"):
        urgency = "routine"
    res = db.escalate(session.get("verified_patient_id"), args.get("reason", ""), urgency)
    res["next_step"] = {
        "emergency": "Staff alerted. The patient must still call 911 now. Do not schedule.",
        "urgent": "A nurse will call the patient back today.",
        "routine": "The front desk will call back within one business day.",
    }[urgency]
    return res, session


_HANDLERS = {
    "verify_patient": _verify, "search_availability": _search, "list_my_appointments": _list,
    "propose_booking": _propose_booking, "propose_cancellation": _propose_cancel,
    "propose_reschedule": _propose_reschedule, "confirm_action": _confirm,
    "get_clinic_info": _info, "escalate_to_human": _escalate,
}


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))
