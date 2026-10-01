"""The code-enforced guarantees. These must hold no matter what the model or the policy says."""
import datetime as dt

from clinic_agent.clinic import ClinicDB
from clinic_agent.tools import execute_tool, new_session


def verified(db, name="David Chen", dob="1972-11-02"):
    s = new_session()
    s["turn"] = 1
    res, s = execute_tool("verify_patient", {"full_name": name, "date_of_birth": dob}, s, db)
    assert res["ok"], res
    return s


def test_record_tools_require_verification():
    db = ClinicDB()
    for name in ["search_availability", "list_my_appointments", "propose_booking", "confirm_action"]:
        res, _ = execute_tool(name, {}, new_session(), db)
        assert res["error"] == "not_verified"


def test_verification_is_vague_and_locks_after_three_failures():
    db, s = ClinicDB(), new_session()
    for i in range(3):
        res, s = execute_tool("verify_patient", {"full_name": "Maria Lopez", "date_of_birth": "1985-03-15"}, s, db)
        assert res["error"] == "no_match" and "date" not in res.get("detail", "").lower().replace("date of birth", "")
    res, s = execute_tool("verify_patient", {"full_name": "Maria Lopez", "date_of_birth": "1985-03-14"}, s, db)
    assert res["error"] == "locked" and s["verified_patient_id"] is None


def test_duplicate_names_resolved_by_dob():
    db = ClinicDB()
    s = verified(db, "john smith", "1994-08-30")
    assert s["verified_patient_id"] == "PT-1006"


def test_cannot_confirm_in_same_turn_as_proposal():
    db = ClinicDB()
    s = verified(db)
    slot = db.search("PT-1002", "follow_up", dt.date(2026, 10, 12), dt.date(2026, 10, 16))[0]
    res, s = execute_tool("propose_booking", {"slot_id": slot, "appointment_type": "follow_up", "reason": "bp"}, s, db)
    assert res["status"] == "PENDING_PATIENT_CONFIRMATION" and not db.events
    res, s = execute_tool("confirm_action", {}, s, db)
    assert res["error"] == "patient_has_not_confirmed" and not db.events
    s["turn"] += 1  # patient replied
    res, s = execute_tool("confirm_action", {}, s, db)
    assert res["ok"] and db.events[0]["op"] == "book"
    res, s = execute_tool("confirm_action", {}, s, db)  # can't replay
    assert res["error"] == "nothing_pending"


def test_cannot_touch_another_patients_appointment():
    db = ClinicDB()
    s = verified(db)  # David
    res, s = execute_tool("propose_cancellation", {"appointment_id": "A-5002"}, s, db)  # John Smith's
    assert res["error"] == "not_found"
    res, s = execute_tool("list_my_appointments", {}, s, db)
    assert res["appointments"] == []


def test_switching_patient_clears_pending_action():
    db = ClinicDB()
    s = verified(db, "Priya Nair", "1990-07-21")
    slot = db.search("PT-1003", "follow_up", dt.date(2026, 10, 12), dt.date(2026, 10, 16))[0]
    _, s = execute_tool("propose_booking", {"slot_id": slot, "appointment_type": "follow_up", "reason": "x"}, s, db)
    _, s = execute_tool("verify_patient", {"full_name": "Leo Nair", "date_of_birth": "2018-05-09"}, s, db)
    assert s["pending_action"] is None


def test_eligibility_enforced_child_and_adult_only_provider():
    db = ClinicDB()
    s = verified(db, "Leo Nair", "2018-05-09")
    res, _ = execute_tool("search_availability", {"appointment_type": "follow_up", "date_from": "2026-10-07",
                                                  "date_to": "2026-10-20", "provider_id": "P2"}, s, db)
    assert res["error"] == "not_eligible"
    res, _ = execute_tool("search_availability", {"appointment_type": "well_child", "date_from": "2026-10-07",
                                                  "date_to": "2026-10-20"}, s, db)
    assert res["slots"] and all(x["provider"] == "Dr. Lena Park" for x in res["slots"])


def test_no_availability_reports_next_open():
    db = ClinicDB()
    s = verified(db, "Aisha Bello", "1979-04-18")
    res, _ = execute_tool("search_availability", {"appointment_type": "dermatology_consult",
                                                  "date_from": "2026-10-07", "date_to": "2026-10-20"}, s, db)
    assert res["slots"] == [] and res["next_available_after_range"]["date"] >= "2026-10-21"


def test_slot_taken_fault_and_atomic_reschedule():
    db = ClinicDB(faults={"book": ["slot_taken"]})
    s = verified(db, "Maria Lopez", "1985-03-14")
    slot = db.search("PT-1001", "follow_up", dt.date(2026, 10, 13), dt.date(2026, 10, 14))[0]
    _, s = execute_tool("propose_reschedule", {"appointment_id": "A-5001", "new_slot_id": slot}, s, db)
    s["turn"] += 1
    res, s = execute_tool("confirm_action", {}, s, db)
    assert not res["ok"]
    assert db.appointments["A-5001"]["status"] == "booked"  # old appointment untouched on failure
    assert not db.events


def test_late_cancellation_note():
    db = ClinicDB()
    s = verified(db, "Aisha Bello", "1979-04-18")
    res, s = execute_tool("propose_cancellation", {"appointment_id": "A-5003"}, s, db)
    assert "fee" in res["policy_note"]
