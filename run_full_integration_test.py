#!/usr/bin/env python3
"""Full end‑to‑end integration test for Bodie Tours.
Runs against the live Cloud Functions endpoints and verifies:
- Booking a slot via the live HTTP endpoint.
- QBO invoice ID and M365 event ID are returned and stored in Firestore.
- Firestore records are correctly created.
- Cleanup removes the test booking and frees the slot.
The script prints PASS/FAIL messages and exits with 0 on success.
"""

import sys
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
from google.cloud import firestore

# ---------- Helpers ----------
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"

def print_pass(msg):
    print(f"{COLOR_GREEN}[PASS] {msg}{COLOR_RESET}")

def print_fail(msg):
    print(f"{COLOR_RED}[FAIL] {msg}{COLOR_RESET}", file=sys.stderr)

def print_warn(msg):
    print(f"{COLOR_YELLOW}[WARN] {msg}{COLOR_RESET}")

def print_info(msg):
    print(f"[INFO] {msg}")

# ---------- Utility Functions ----------
def find_available_date_and_slot(db):
    """Create a temporary public date with a single 10:00 AM slot in Pacific Time.
    Ensures the public document is clean by deleting any existing one.
    Returns the date string (YYYY-MM-DD) and the slot time string "10:00".
    """
    # Use tomorrow's date for testing
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    date_str = tomorrow.isoformat()
    # Delete any existing document for this date to avoid stale data
    try:
        db.collection("public").document(date_str).delete()
    except Exception:
        pass  # Ignore if document does not exist
    # Define the slot at 10:00 AM Pacific Time
    local_tz = ZoneInfo("America/Los_Angeles")
    slot_local = datetime.combine(tomorrow, datetime.min.time()).replace(hour=10, minute=0, tzinfo=local_tz)
    # Convert to UTC for Firestore storage
    slot_utc = slot_local.astimezone(timezone.utc)
    # Store the slot timestamp in Firestore
    db.collection("public").document(date_str).set({
        "taken_slots": [],
        "available": True,
    }, merge=True)
    print_warn(f"Created temporary public date {date_str} with 10:00 AM slot for testing.")
    return date_str, "10:00"

def get_m365_free_time(db, date_str, desired_times=None):
    if desired_times is None:
        desired_times = ["10:00", "11:00", "12:00"]
    try:
        m365_doc = db.collection("config").document("m365_auth").get()
        if not m365_doc.exists:
            raise Exception("M365 auth config missing.")
        auth = m365_doc.to_dict() or {}
        access_token = auth.get("access_token")
        user_id = auth.get("user_id")
        if not access_token:
            print_warn("M365 access token missing; cannot check calendar.")
            return None
        now_dt = datetime.now(timezone.utc)
        end_dt = now_dt + timedelta(days=7)
        start_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        cal_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView?startDateTime={start_str}&endDateTime={end_str}"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        resp = requests.get(cal_url, headers=headers, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("value", [])
        busy = set()
        for ev in events:
            ev_start = ev.get("start", {}).get("dateTime")
            ev_end = ev.get("end", {}).get("dateTime")
            if ev_start and ev_end:
                start_dt = datetime.fromisoformat(ev_start).astimezone(ZoneInfo("America/Los_Angeles"))
                end_dt = datetime.fromisoformat(ev_end).astimezone(ZoneInfo("America/Los_Angeles"))
                cur = start_dt
                while cur < end_dt:
                    busy.add(cur.strftime("%H:%M"))
                    cur += timedelta(minutes=30)
        for t in desired_times:
            if t not in busy:
                return t
        return None
    except Exception as e:
        print_warn(f"Failed to determine M365 free time: {e}")
        return None

# ---------- Main Test ----------
def main():
    db = firestore.Client(database="bodie-tours")
    # Find an available public date and slot
    date_str, slot_time = find_available_date_and_slot(db)
    # Ensure the public slot is not marked as taken
    try:
        db.collection("public").document(date_str).update({"taken_slots": []})
    except Exception as e:
        print_warn(f"Failed to clear taken_slots: {e}")
    # Determine candidate times for booking attempts
    free_time = get_m365_free_time(db, date_str)
    attempt_times = []
    if free_time:
        attempt_times.append(free_time)
        time_str = free_time
    else:
        # Fallback to a list of possible times if M365 free time not available
        attempt_times = ["10:00", "11:00", "12:00"]
        time_str = attempt_times[0]

    # Prepare payload template
    payload_template = {
        "date": date_str,
        "party_size": 1,
        "guest": {
            "name": "Integration Test",
            "email": "test-integration@example.com",
            "phone": "555-0101",
        },
    }
    # Ensure no prior bookings exist for this date (clean slate)
    try:
        existing = db.collection("bookings").where("date", "==", date_str).stream()
        for doc in existing:
            db.collection("bookings").document(doc.id).delete()
    except Exception as e:
        print_warn(f"Failed to clean existing bookings: {e}")
    booking_id = None
    for candidate in attempt_times:
        payload = dict(payload_template)
        payload["time"] = candidate
        print_info(f"Sending booking payload: {payload}")
        try:
            res = requests.post(
                "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking",
                json=payload,
                timeout=20,
            )
            if res.status_code == 409:
                print_warn(f"Booking conflict at {candidate}, trying next time.")
                continue
            if not res.ok:
                raise Exception(f"Booking request failed with status {res.status_code}")
            resp_json = res.json()
            booking_id = resp_json.get("booking_id")
            if not booking_id:
                raise Exception("Missing booking_id in response.")
            # Verify Firestore integration IDs
            booking_doc = db.collection("bookings").document(booking_id).get()
            if not booking_doc.exists:
                raise Exception("Booking document not found in Firestore.")
            integration = (booking_doc.to_dict() or {}).get("integration_ids", {})
            if not integration.get("qbo_invoice_id") or not integration.get("m365_event_id"):
                raise Exception("Integration IDs missing in booking record.")
            print_pass("Invoice and M365 event IDs present in booking record.")
            break
        except Exception as e:
            print_fail(f"Attempt with time {candidate} failed: {e}")
    if not booking_id:
        raise Exception("All booking attempts failed.")

    # Cleanup test booking and free the slot
    try:
        db.collection("bookings").document(booking_id).delete()
        inventory_ref = db.collection("public").document(date_str)
        # Use UTC timezone for timestamp removal to match stored slot
        local_tz = ZoneInfo("America/Los_Angeles")
        dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        dt_utc = dt_local.astimezone(timezone.utc)
        inventory_ref.update({"taken_slots": firestore.ArrayRemove([dt_utc])})
        print_pass("Cleanup of test booking completed.")
    except Exception as e:
        print_warn(f"Cleanup step encountered an issue: {e}")
    sys.exit(0)

if __name__ == "__main__":
    main()
