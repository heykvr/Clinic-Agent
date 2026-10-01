"""In-memory clinic: seed data, scheduling rules and the one place state is mutated.

Everything here is deterministic (seeded) so eval runs are reproducible and
deterministic checks can compare the final state against the seed.

Business rules that must never be wrong (eligibility, ownership, double-booking)
live HERE, not in the prompt. The model can ask for anything; this layer decides.
"""
from __future__ import annotations

import copy
import datetime as dt
import random
from dataclasses import dataclass, field

# Assumption: the clinic's "now" is pinned so relative dates ("this Friday") are
# reproducible across runs. Wednesday 7 Oct 2026, 08:30 local clinic time.
CLINIC_NOW = dt.datetime(2026, 10, 7, 8, 30)
HORIZON_DAYS = 28  # schedule is open four weeks out
EMERGENCY_NUMBER = "911"

APPOINTMENT_TYPES = {
    "new_patient": {"minutes": 60, "label": "New patient visit"},
    "follow_up": {"minutes": 30, "label": "Follow-up visit"},
    "annual_physical": {"minutes": 60, "label": "Annual physical"},
    "sick_visit": {"minutes": 30, "label": "Sick visit (same/next-day problems)"},
    "well_child": {"minutes": 30, "label": "Well-child check"},
    "dermatology_consult": {"minutes": 30, "label": "Dermatology consult"},
}

PROVIDERS = {
    "P1": {"name": "Dr. Anita Rao", "specialty": "Family medicine", "weekdays": [0, 1, 2, 3, 4],
           "min_age": 0, "max_age": 200,
           "types": ["new_patient", "follow_up", "annual_physical", "sick_visit"]},
    "P2": {"name": "Dr. James Okafor", "specialty": "Internal medicine (adults only)", "weekdays": [0, 2, 4],
           "min_age": 18, "max_age": 200,
           "types": ["new_patient", "follow_up", "annual_physical", "sick_visit"]},
    "P3": {"name": "Dr. Lena Park", "specialty": "Pediatrics (under 18)", "weekdays": [1, 3],
           "min_age": 0, "max_age": 17,
           "types": ["new_patient", "follow_up", "sick_visit", "well_child"]},
    "P4": {"name": "Dr. Samuel Reyes", "specialty": "Dermatology", "weekdays": [3],
           "min_age": 12, "max_age": 200, "types": ["dermatology_consult"]},
}

PATIENTS = {
    "PT-1001": {"name": "Maria Lopez", "dob": "1985-03-14", "phone": "555-0101"},
    "PT-1002": {"name": "David Chen", "dob": "1972-11-02", "phone": "555-0102"},
    "PT-1003": {"name": "Priya Nair", "dob": "1990-07-21", "phone": "555-0103"},
    "PT-1004": {"name": "Leo Nair", "dob": "2018-05-09", "phone": "555-0103"},
    "PT-1005": {"name": "John Smith", "dob": "1960-01-01", "phone": "555-0105"},
    "PT-1006": {"name": "John Smith", "dob": "1994-08-30", "phone": "555-0106"},
    "PT-1007": {"name": "Robert Hughes", "dob": "1948-12-05", "phone": "555-0107"},
    "PT-1008": {"name": "Aisha Bello", "dob": "1979-04-18", "phone": "555-0108"},
}

SEED_APPOINTMENTS = [
    {"id": "A-5001", "patient_id": "PT-1001", "provider_id": "P1", "type": "follow_up",
     "date": "2026-10-09", "time": "10:00", "reason": "blood pressure check"},
    {"id": "A-5002", "patient_id": "PT-1005", "provider_id": "P2", "type": "annual_physical",
     "date": "2026-10-12", "time": "09:00", "reason": "annual physical"},
    {"id": "A-5003", "patient_id": "PT-1008", "provider_id": "P1", "type": "follow_up",
     "date": "2026-10-07", "time": "15:00", "reason": "medication review"},
]

CLINIC_INFO = {
    "hours": "Monday-Friday 9:00-17:00 (closed 12:00-13:30 for lunch). Closed weekends.",
    "address": "214 Riverside Avenue, Suite 3. Free parking behind the building.",
    "cancellation_policy": "Please cancel at least 24 hours ahead. Cancellations or no-shows "
                           "inside 24 hours may incur a $25 fee, which the front desk can waive.",
    "nurse_line": "The nurse advice line (555-0199) is staffed 8:00-18:00 on weekdays for "
                  "clinical questions. Outside those hours, call 911 for emergencies.",
    "billing": "Billing questions: billing office 555-0150, Mon-Fri 9:00-16:00. "
               "The scheduling assistant cannot see bills or insurance details.",
    "what_to_bring": "Photo ID, insurance card, list of current medications. New patients "
                     "should arrive 15 minutes early.",
    "insurance": "The clinic accepts most major plans; the billing office can confirm a specific plan.",
}

_SLOT_TIMES = [f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 30)
               if not (12 * 60 <= h * 60 + m < 13 * 60 + 30)]


def _age_on(dob: str, on: dt.date) -> int:
    b = dt.date.fromisoformat(dob)
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


def _norm_name(s: str) -> str:
    return " ".join(s.lower().replace(".", " ").replace(",", " ").split())


@dataclass
class ClinicDB:
    """Per-conversation clinic state. Cheap to construct; one per eval trial."""
    faults: dict = field(default_factory=dict)  # {"book": ["slot_taken"], ...}

    def __post_init__(self):
        self.now = CLINIC_NOW
        self.today = CLINIC_NOW.date()
        self.patients = copy.deepcopy(PATIENTS)
        self.appointments = {a["id"]: {**a, "status": "booked"} for a in copy.deepcopy(SEED_APPOINTMENTS)}
        self.escalations: list[dict] = []
        self.events: list[dict] = []  # audit log of every successful write
        self._next_appt = 6001
        self._faults = {k: list(v) for k, v in (self.faults or {}).items()}
        self.taken: set[str] = set()
        rng = random.Random(7)
        for day in range(HORIZON_DAYS):
            d = self.today + dt.timedelta(days=day)
            for pid, p in PROVIDERS.items():
                if d.weekday() not in p["weekdays"]:
                    continue
                for t in _SLOT_TIMES:
                    sid = self.slot_id(pid, d, t)
                    # Dermatology is fully booked for the next two weeks (no-availability case).
                    if pid == "P4" and day < 14:
                        self.taken.add(sid)
                    # Same-day: only a few afternoon slots left; otherwise ~55% pre-booked.
                    elif day == 0 and (t < "14:00" or rng.random() < 0.7):
                        self.taken.add(sid)
                    elif rng.random() < 0.55:
                        self.taken.add(sid)
        for a in self.appointments.values():
            for sid in self._covered_slots(a["provider_id"], a["date"], a["time"], a["type"]) or []:
                self.taken.add(sid)
        self.initial_appointments = copy.deepcopy(self.appointments)

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def slot_id(pid: str, d: dt.date, t: str) -> str:
        return f"{pid}-{d.isoformat()}-{t.replace(':', '')}"

    @staticmethod
    def parse_slot(sid: str):
        try:
            pid, y, m, d, hm = sid.split("-")
            return pid, dt.date(int(y), int(m), int(d)), f"{hm[:2]}:{hm[2:]}"
        except Exception:
            return None

    def _covered_slots(self, pid, date, time, appt_type):
        d = dt.date.fromisoformat(date) if isinstance(date, str) else date
        n = APPOINTMENT_TYPES[appt_type]["minutes"] // 30
        if time not in _SLOT_TIMES:
            return None
        i = _SLOT_TIMES.index(time)
        times = _SLOT_TIMES[i:i + n]
        if len(times) < n:
            return None
        # 60-minute visits cannot straddle lunch.
        if n == 2:
            t0 = dt.datetime.combine(d, dt.time.fromisoformat(times[0]))
            t1 = dt.datetime.combine(d, dt.time.fromisoformat(times[1]))
            if (t1 - t0) != dt.timedelta(minutes=30):
                return None
        return [self.slot_id(pid, d, t) for t in times]

    def age(self, patient_id: str) -> int:
        return _age_on(self.patients[patient_id]["dob"], self.today)

    def _fault(self, op: str) -> str | None:
        q = self._faults.get(op)
        return q.pop(0) if q else None

    def describe_slot(self, sid: str) -> dict:
        pid, d, t = self.parse_slot(sid)
        return {"slot_id": sid, "provider": PROVIDERS[pid]["name"], "date": d.isoformat(),
                "weekday": d.strftime("%A"), "time": t}

    def describe_appt(self, a: dict) -> dict:
        d = dt.date.fromisoformat(a["date"])
        return {"appointment_id": a["id"], "type": a["type"], "provider": PROVIDERS[a["provider_id"]]["name"],
                "date": a["date"], "weekday": d.strftime("%A"), "time": a["time"], "status": a["status"]}

    # ---- reads ---------------------------------------------------------
    def find_patient(self, name: str, dob: str) -> list[str]:
        return [pid for pid, p in self.patients.items()
                if _norm_name(p["name"]) == _norm_name(name) and p["dob"] == dob]

    def eligible(self, patient_id: str, pid: str, appt_type: str) -> str | None:
        p = PROVIDERS[pid]
        if appt_type not in p["types"]:
            return f"{p['name']} does not offer {appt_type}."
        age = self.age(patient_id)
        if not (p["min_age"] <= age <= p["max_age"]):
            return f"{p['name']} only sees patients aged {p['min_age']}-{min(p['max_age'], 120)}."
        return None

    def search(self, patient_id, appt_type, date_from, date_to, provider_id=None, part_of_day="any", limit=8):
        last_open = self.today + dt.timedelta(days=HORIZON_DAYS - 1)
        providers = [provider_id] if provider_id else list(PROVIDERS)
        out = []
        d = max(date_from, self.today)
        while d <= min(date_to, last_open):
            for pid in providers:
                if PROVIDERS[pid]["weekdays"].count(d.weekday()) == 0:
                    continue
                if self.eligible(patient_id, pid, appt_type):
                    continue
                for t in _SLOT_TIMES:
                    if d == self.today and dt.datetime.combine(d, dt.time.fromisoformat(t)) <= self.now:
                        continue
                    if part_of_day == "morning" and t >= "12:00":
                        continue
                    if part_of_day == "afternoon" and t < "12:00":
                        continue
                    cov = self._covered_slots(pid, d, t, appt_type)
                    if cov and not any(s in self.taken for s in cov):
                        out.append(self.slot_id(pid, d, t))
            d += dt.timedelta(days=1)
        out.sort(key=lambda s: (self.parse_slot(s)[1], self.parse_slot(s)[2]))
        return out[:limit]

    def list_for(self, patient_id):
        return [a for a in self.appointments.values()
                if a["patient_id"] == patient_id and a["status"] == "booked"
                and dt.date.fromisoformat(a["date"]) >= self.today]

    # ---- writes (the only mutation points) -----------------------------
    def book(self, patient_id, slot_id, appt_type, reason) -> dict:
        parsed = self.parse_slot(slot_id)
        if not parsed or parsed[0] not in PROVIDERS:
            return {"ok": False, "error": "unknown_slot", "detail": "Slot id not recognised. Search again."}
        pid, d, t = parsed
        if appt_type not in APPOINTMENT_TYPES:
            return {"ok": False, "error": "unknown_type"}
        why = self.eligible(patient_id, pid, appt_type)
        if why:
            return {"ok": False, "error": "not_eligible", "detail": why}
        cov = self._covered_slots(pid, d, t, appt_type)
        if not cov or any(s in self.taken for s in cov) or d < self.today:
            return {"ok": False, "error": "slot_unavailable", "detail": "That time is no longer available."}
        if self._fault("book") == "slot_taken":
            self.taken.update(cov)  # someone else grabbed it a moment ago
            return {"ok": False, "error": "slot_unavailable",
                    "detail": "That time was just taken by another booking. Search again."}
        aid = f"A-{self._next_appt}"
        self._next_appt += 1
        self.taken.update(cov)
        appt = {"id": aid, "patient_id": patient_id, "provider_id": pid, "type": appt_type,
                "date": d.isoformat(), "time": t, "reason": reason, "status": "booked"}
        self.appointments[aid] = appt
        self.events.append({"op": "book", "appointment_id": aid, "patient_id": patient_id})
        return {"ok": True, "appointment": self.describe_appt(appt)}

    def cancel(self, patient_id, appointment_id) -> dict:
        a = self.appointments.get(appointment_id)
        # Same message for "doesn't exist" and "not yours": never confirm another patient's record.
        if not a or a["patient_id"] != patient_id or a["status"] != "booked":
            return {"ok": False, "error": "not_found", "detail": "No such upcoming appointment for this patient."}
        if self._fault("cancel") == "system_error":
            return {"ok": False, "error": "system_error", "detail": "Scheduling system unavailable. Try later."}
        a["status"] = "cancelled"
        for sid in self._covered_slots(a["provider_id"], a["date"], a["time"], a["type"]) or []:
            self.taken.discard(sid)
        start = dt.datetime.combine(dt.date.fromisoformat(a["date"]), dt.time.fromisoformat(a["time"]))
        late = (start - self.now) < dt.timedelta(hours=24)
        self.events.append({"op": "cancel", "appointment_id": appointment_id, "patient_id": patient_id})
        res = {"ok": True, "cancelled": self.describe_appt(a)}
        if late:
            res["note"] = "Late cancellation (<24h): a $25 fee may apply; front desk can waive."
        return res

    def reschedule(self, patient_id, appointment_id, new_slot_id) -> dict:
        a = self.appointments.get(appointment_id)
        if not a or a["patient_id"] != patient_id or a["status"] != "booked":
            return {"ok": False, "error": "not_found", "detail": "No such upcoming appointment for this patient."}
        # Atomic: book the new slot first; only then release the old one.
        res = self.book(patient_id, new_slot_id, a["type"], a.get("reason", ""))
        if not res["ok"]:
            return res
        self.events.pop()  # replace the inner "book" event with a single reschedule event
        new_id = res["appointment"]["appointment_id"]
        a["status"] = "rescheduled"
        for sid in self._covered_slots(a["provider_id"], a["date"], a["time"], a["type"]) or []:
            self.taken.discard(sid)
        self.appointments[new_id]["rescheduled_from"] = appointment_id
        self.events.append({"op": "reschedule", "appointment_id": appointment_id,
                            "new_appointment_id": new_id, "patient_id": patient_id})
        return {"ok": True, "old": self.describe_appt(a), "new": res["appointment"]}

    def escalate(self, patient_id, reason, urgency) -> dict:
        tid = f"T-{len(self.escalations) + 1:03d}"
        self.escalations.append({"id": tid, "patient_id": patient_id, "reason": reason, "urgency": urgency})
        return {"ok": True, "ticket": tid}
