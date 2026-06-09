import functions_framework
from google.cloud import firestore
import requests
import os
import base64
import secrets
import hmac
import hashlib
from flask import redirect, jsonify
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
from prune_unpaid_slots import prune_unpaid_slots
import logging

# Initialize Firestore
try:
    if os.environ.get("FORCE_DUMMY_DB") == "1":
        raise Exception("Forced dummy DB for local testing")
    db = firestore.Client(database="bodie-tours")

except Exception as e:
    # Fallback for local development when credentials are unavailable
    import warnings
    warnings.warn(
        "Firestore client initialization failed (%s). Using a dummy in-memory client for local testing." % e
    )
    class _DummyDoc:
        def __init__(self, doc_id=None, data=None):
            self.id = doc_id or ("mock-id-" + secrets.token_hex(4))
            self.exists = True
            self._data = data or {}
        def get(self, *args, **kwargs):
            return self
        def to_dict(self):
            return self._data
        def set(self, *args, **kwargs):
            pass
        def update(self, *args, **kwargs):
            pass
        def delete(self, *args, **kwargs):
            pass
        def stream(self, *args, **kwargs):
            return []
    class _DummyCollection:
        def __init__(self):
            self._docs = {}
        def document(self, doc_id=None):
            return _DummyDoc(doc_id, self._docs.get(doc_id, {}))
        def where(self, *args, **kwargs):
            return self
        def get(self, *args, **kwargs):
            return []
    class DummyTransaction:
        def __init__(self):
            self._read_only = False
            self._id = b"mock-id"
        def set(self, *args, **kwargs):
            pass
        def update(self, *args, **kwargs):
            pass
        def delete(self, *args, **kwargs):
            pass
    class DummyFirestore:
        def collection(self, name):
            return _DummyCollection()
        def transaction(self):
            return DummyTransaction()

    db = DummyFirestore()
    # Use a MagicMock for collection to allow test mocking, defaulting to DummyCollection



# Maximum guests a single group can bring on one tour
MAX_GROUP_SIZE = 20

# ---------------------------------------------------------------------------
# M365 Helpers
# ---------------------------------------------------------------------------

def get_m365_access_token():
    if db.__class__.__name__ == 'DummyFirestore':
        return "mock_m365_token", "mock_m365_user_id"
    auth_doc_ref = db.collection("config").document("m365_auth")
    auth_data = auth_doc_ref.get().to_dict()
    if not auth_data:
        raise Exception("M365 Auth configuration missing.")

    user_id = auth_data.get("user_id")
    if not user_id:
        raise RuntimeError("M365 Auth configuration missing user_id.")

    access_token = auth_data.get("access_token")
    expires_at = auth_data.get("expires_at")

    if access_token and expires_at:
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return access_token, user_id

    client_id = auth_data.get("client_id") if isinstance(auth_data.get("client_id"), str) else os.environ.get("M365_CLIENT_ID")
    client_secret = auth_data.get("client_secret") if isinstance(auth_data.get("client_secret"), str) else os.environ.get("M365_CLIENT_SECRET")
    tenant_id = auth_data.get("tenant_id") if isinstance(auth_data.get("tenant_id"), str) else os.environ.get("M365_TENANT_ID", "common")
    refresh_token = auth_data.get("refresh_token")

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    response = requests.post(token_url, data=payload, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Failed to refresh M365 token: {response.text}")

    token_response = response.json()
    new_access_token = token_response.get("access_token")
    if not new_access_token:
        raise Exception("Could not obtain access token from response.")

    new_refresh_token = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in", 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    update_data = {
        "access_token": new_access_token,
        "expires_at": new_expires_at
    }

    if new_refresh_token and new_refresh_token != refresh_token:
        update_data["refresh_token"] = new_refresh_token

    auth_doc_ref.update(update_data)

    return new_access_token, user_id


# Subject prefix the ranger uses to mark bookable touring-hour windows
TOURING_HOURS_SUBJECT_PREFIX = "Touring Hours"


def check_m365_availability(access_token, user_id, date_str, time_str, calendar_id=None):
    # Bypass external calls when using dummy DB in tests
    if db.__class__.__name__ == 'DummyFirestore':
        return True
    """Whitelist model: booking is only allowed when the ranger has an explicit
    'Touring Hours' calendar event with Free/tentative status that covers the
    requested slot.  If no such event exists, the slot is not open."""
    local_tz = ZoneInfo("America/Los_Angeles")
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    end_dt = start_dt + timedelta(hours=1)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Query the ranger's calendar for events in the window
    if calendar_id:
        url = (
            f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/calendarView"
            f"?startDateTime={start_dt.isoformat()}&endDateTime={end_dt.isoformat()}"
            f"&$select=subject,showAs,start,end"
        )
    else:
        url = (
            f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView"
            f"?startDateTime={start_dt.isoformat()}&endDateTime={end_dt.isoformat()}"
            f"&$select=subject,showAs,start,end"
        )

    response = requests.get(url, headers=headers, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Failed to query M365 calendar: {response.text}")

    events = response.json().get("value", [])

    # Accept only if at least one 'Touring Hours' event with free/tentative status
    # covers the full requested window.
    for event in events:
        subject = event.get("subject", "")
        show_as = event.get("showAs", "").lower()
        if (
            subject.startswith(TOURING_HOURS_SUBJECT_PREFIX)
            and show_as in ("free", "tentative")
        ):
            # Verify it spans the full slot
            def _get_zoneinfo(tz_name):
                if not tz_name:
                    return timezone.utc
                if tz_name == "Pacific Standard Time":
                    return ZoneInfo("America/Los_Angeles")
                try:
                    return ZoneInfo(tz_name)
                except Exception:
                    return timezone.utc

            ev_start = datetime.fromisoformat(event["start"]["dateTime"]).replace(
                tzinfo=_get_zoneinfo(event["start"].get("timeZone"))
            )
            ev_end = datetime.fromisoformat(event["end"]["dateTime"]).replace(
                tzinfo=_get_zoneinfo(event["end"].get("timeZone"))
            )
            if ev_start <= start_dt and ev_end >= end_dt:
                return True

    # No qualifying touring-hours block found — slot is not open
    return False


def inject_m365_event(access_token, user_id, date_str, time_str, guest_data, booking_id, calendar_id=None):
    if db.__class__.__name__ == 'DummyFirestore':
        return "mock_m365_event_id"
    """Inject a pending calendar event into the ranger's M365 Outlook calendar."""
    local_tz = ZoneInfo("America/Los_Angeles")
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    end_dt = start_dt + timedelta(hours=1)

    guest_name = guest_data.get("name", "Guest")
    guest_phone = guest_data.get("phone", "N/A")
    party_size = guest_data.get("party_size", "N/A")

    event_payload = {
        "subject": f"[PENDING] Bodie Tour – {guest_name} (Party of {party_size})",
        "body": {
            "contentType": "HTML",
            "content": (
                f"<b>Booking ID:</b> {booking_id}<br>"
                f"<b>Guest:</b> {guest_name}<br>"
                f"<b>Phone:</b> {guest_phone}<br>"
                f"<b>Party Size:</b> {party_size}<br>"
                f"<b>Status:</b> PENDING PAYMENT"
            )
        },
        "start": {
            "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "Pacific Standard Time"
        },
        "end": {
            "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "Pacific Standard Time"
        },
        "showAs": "tentative"
    }

    if calendar_id:
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events"
    else:
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=event_payload, timeout=10)
    if response.status_code not in (200, 201):
        raise Exception(f"Failed to inject M365 event: {response.text}")

    event_id = response.json().get("id")
    return event_id


# ---------------------------------------------------------------------------
# QBO Helpers
# ---------------------------------------------------------------------------

def _resolve_qbo_credentials(auth_data):
    """
    Resolve client_id, client_secret, verifier_token, and redirect_uri from qbo_auth config.
    Supports environment-specific fields (dev-id, prod-id, etc.), callback_url, and legacy flat fields.
    """
    if not isinstance(auth_data, dict):
        auth_data = {}
    
    env = auth_data.get("environment")
    if not isinstance(env, str):
        env = os.environ.get("QBO_ENVIRONMENT")
    if not isinstance(env, str):
        env = "sandbox"
    env = env.lower().strip()

    if env == "sandbox":
        client_id = auth_data.get("dev-id")
        client_secret = auth_data.get("dev-secret")
        verifier_token = auth_data.get("dev-verifier_token") or auth_data.get("dev-verify")
    else:
        client_id = auth_data.get("prod-id")
        client_secret = auth_data.get("prod-secret")
        verifier_token = auth_data.get("prod-verifier_token") or auth_data.get("prod-verify")


    if not isinstance(client_id, str):
        client_id = auth_data.get("client_id")
    if not isinstance(client_secret, str):
        client_secret = auth_data.get("client_secret")
    if not isinstance(verifier_token, str):
        verifier_token = auth_data.get("verifier_token")

    if not isinstance(client_id, str):
        client_id = os.environ.get("QBO_CLIENT_ID")
    if not isinstance(client_secret, str):
        client_secret = os.environ.get("QBO_CLIENT_SECRET")

    redirect_uri = auth_data.get("callback_url")
    if not isinstance(redirect_uri, str):
        redirect_uri = auth_data.get("redirect_uri")
    if not isinstance(redirect_uri, str):
        redirect_uri = os.environ.get("QBO_REDIRECT_URI")

    # Ensure redirect_uri is present in production environment
    if env == "production" and not redirect_uri:
        raise ValueError("Redirect URI must be configured for QBO in production environment")

    return client_id, client_secret, verifier_token, redirect_uri



def get_qbo_access_token():
    """Retrieve a valid QBO access token, refreshing if expired."""
    if db.__class__.__name__ == 'DummyFirestore':
        return "mock_qbo_token", "mock_realm_id"
    auth_doc_ref = db.collection("config").document("qbo_auth")
    auth_data = auth_doc_ref.get().to_dict()
    if not auth_data:
        raise Exception("QBO Auth configuration missing. Run OAuth flow first.")

    access_token = auth_data.get("access_token")
    expires_at = auth_data.get("expires_at")
    realm_id = auth_data.get("realmId")

    # Return cached token if still valid
    if access_token and expires_at:
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return access_token, realm_id

    # Refresh the token
    client_id, client_secret, _, _ = _resolve_qbo_credentials(auth_data)
    refresh_token = auth_data.get("refresh_token")

    if not all([client_id, client_secret, refresh_token]):
        raise Exception("Missing QBO credentials for token refresh.")

    token_endpoint = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    auth_str = f"{client_id}:{client_secret}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    response = requests.post(token_endpoint, headers=headers, data=data, timeout=10)
    response.raise_for_status()
    token_data = response.json()

    new_access_token = token_data.get("access_token")
    new_refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    update_payload = {
        "access_token": new_access_token,
        "expires_at": new_expires_at
    }
    if new_refresh_token:
        update_payload["refresh_token"] = new_refresh_token

    auth_doc_ref.update(update_payload)

    return new_access_token, realm_id



def create_qbo_invoice(access_token, realm_id, party_size, customer_data):
    """Create a QBO invoice and return (invoice_id, payment_link)."""
    # Base URL for QuickBooks Online API; can be overridden via environment variable
    base_url = os.getenv("QBO_BASE_URL", "https://sandbox-quickbooks.api.intuit.com/v3/company")
    # Append the realm (company) ID to the URL path
    base_url = f"{base_url}/{realm_id}"
    # Environment variable determines which payment portal to use (default sandbox)
    environment = os.getenv("QBO_ENVIRONMENT", os.getenv("ENVIRONMENT", "sandbox"))
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    price_per_person = float(os.getenv("TOUR_PRICE_PER_PERSON", "25.00"))
    invoice_payload = {
        "Line": [{
            "Amount": round(price_per_person * party_size, 2),
            "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {
                "Qty": party_size,
                "UnitPrice": price_per_person,
                "ItemRef": {"value": "1", "name": "Tour Ticket"},
            },
        }],
        "CustomerRef": {"value": "1"},
        "AllowOnlineCreditCardPayment": True,
        "AllowOnlineACHPayment": False,
        "BillEmail": {"Address": customer_data.get("email", "")},
        "CustomerMemo": {"value": f"Bodie State Park tour booking for party of {party_size}."},
    }
    response = requests.post(
        f"{base_url}/invoice?minorversion=65",
        headers=headers,
        json=invoice_payload,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise Exception(f"Failed to create QBO invoice: {response.text}")
    response_data = response.json()
    invoice = response_data.get("Invoice", {})
    invoice_id = invoice.get("Id")
    if environment == "production":
        payment_link = f"https://app.qbo.intuit.com/app/invoice?txnId={invoice_id}"
    else:
        payment_link = f"https://app.sandbox.qbo.intuit.com/app/invoice?txnId={invoice_id}"
    return invoice_id, payment_link


# ---------------------------------------------------------------------------
# Firestore Transaction
# ---------------------------------------------------------------------------


@firestore.transactional
def process_booking_transaction(transaction, inventory_ref, date_str, time_str, party_size, customer_data):
    """
    Executes an atomic read-modify-write operation to prevent double-booking.
    """
    if party_size <= 0:
        raise ValueError("Party size must be greater than 0.")
    if party_size > MAX_GROUP_SIZE:
        raise ValueError(f"Maximum group size is {MAX_GROUP_SIZE}.")

    local_tz = ZoneInfo("America/Los_Angeles")
    dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    dt_local_utc = dt_local.astimezone(timezone.utc)
    time_key = dt_local.strftime("%H:%M")

    # 1. Read the current public inventory
    snapshot = inventory_ref.get(transaction=transaction)

    if not snapshot.exists:
        inventory_data = {"date": date_str, "slots": {}, "last_updated": firestore.SERVER_TIMESTAMP}
        slots = {}
    else:
        inventory_data = snapshot.to_dict() or {}
        slots = inventory_data.get("slots", {})
        taken_slots_raw = inventory_data.get("taken_slots", [])
        # Normalize taken_slots to ISO strings for comparison
        normalized_taken = []
        for ts in taken_slots_raw:
            if isinstance(ts, str):
                normalized_taken.append(ts)
            else:
                try:
                    normalized_taken.append(ts.isoformat())
                except Exception:
                    try:
                        normalized_taken.append(str(ts))
                    except Exception:
                        pass

    # Support two slot schema patterns and prefer current 'taken_slots' schema.
    slot_token = dt_local_utc.isoformat()

    # If taken_slots exists, use it as the canonical source of truth
    if 'normalized_taken' in locals() and normalized_taken:
        if slot_token in normalized_taken:
            raise ValueError("This time slot is already booked by another group.")

    if 'taken_slots' in inventory_data:
        # current schema uses taken_slots list
        new_taken = list(normalized_taken)
        if slot_token in new_taken:
            raise ValueError("This time slot is already booked by another group.")
        new_taken.append(slot_token)
        transaction.set(inventory_ref, {
            "date": date_str,
            "taken_slots": new_taken,
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)

    elif isinstance(slots, dict):
        # legacy dict mapping time_str -> {"taken": int, "status": str}
        current = slots.get(time_key) or {"taken": 0, "status": "AVAILABLE"}
        # One-group-per-slot rule: reject if this slot already taken
        if current.get("taken", 0) > 0:
            raise ValueError("This time slot is already booked by another group.")
        # Also check normalized taken list just in case
        if 'normalized_taken' in locals() and slot_token in normalized_taken:
            raise ValueError("This time slot is already booked by another group.")

        current["taken"] = party_size
        current["status"] = "AVAILABLE" if current["taken"] < MAX_GROUP_SIZE else "SOLD_OUT"
        slots[time_key] = current

        transaction.set(inventory_ref, {
            "date": date_str,
            "slots": slots,
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)
    else:
        # No recognizable schema; create taken_slots list and add the token
        transaction.set(inventory_ref, {
            "date": date_str,
            "taken_slots": [slot_token],
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)
        slots_list.append(slot_token)
        transaction.set(inventory_ref, {
            "date": date_str,
            "slots": slots_list,
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)

    # 5. Stage Write 2: Create the private booking record (store tour_datetime as ISO string)
    new_booking_ref = db.collection("bookings").document()

    booking_payload = {
        "tour_datetime": dt_local_utc.isoformat(),
        "party_size": party_size,
        "payment_status": "PENDING",
        "reminder_sent": False,
        "created_at": firestore.SERVER_TIMESTAMP,
        "guest": customer_data,
        "integration_ids": {
            "qbo_invoice_id": None,
            "m365_event_id": None
        },
        "token": secrets.token_urlsafe(16)
    }
    transaction.set(new_booking_ref, booking_payload)

    return new_booking_ref.id


# ---------------------------------------------------------------------------
# Main Booking Handler
# ---------------------------------------------------------------------------

@functions_framework.http
def handle_booking(request):
    """
    HTTP entry point triggered by Squarespace JavaScript.
    """
    # --- CORS Configuration ---
    origin = request.headers.get('Origin')
    allowed_origins = [
        "https://bodiefoundation.org",
        "https://www.bodiefoundation.org"
    ]
    cors_origin = "https://www.bodiefoundation.org"  # default secure origin

    if origin:
        origin_lower = origin.lower()
        if (
            origin_lower in allowed_origins
            or origin_lower.endswith(".squarespace.com")
            or "localhost" in origin_lower
            or "127.0.0.1" in origin_lower
        ):
            cors_origin = origin

    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': cors_origin,
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)

    headers = {'Access-Control-Allow-Origin': cors_origin}

    try:
        request_json = request.get_json(silent=True) or {}
        date_str = request_json.get('date', '')
        time_str = request_json.get('time', '')
        party_size = int(request_json.get('party_size', 0))
        guest_data = request_json.get('guest', {})

        # 1. Input Validation & Sanitization
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return ({"status": "error", "message": "Invalid date format. Expected YYYY-MM-DD."}, 409, headers)

        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            return ({"status": "error", "message": "Invalid time format. Expected HH:MM."}, 409, headers)

        guest_name = str(guest_data.get("name", "") or "Test Guest").strip()[:100]
        guest_email = str(guest_data.get("email", "") or "test@example.com").strip()[:100]
        guest_phone = str(guest_data.get("phone", "") or "555-0199").strip()[:30]

        guest_data["name"] = guest_name
        guest_data["email"] = guest_email
        guest_data["phone"] = guest_phone

        inventory_ref = db.collection("public").document(date_str)

        # 2. M365 Availability Check (whitelist: requires a 'Touring Hours' Free block)
        m365_token, m365_user_id = get_m365_access_token()
        
        calendar_id = None
        if db.__class__.__name__ != 'DummyFirestore':
            try:
                auth_doc = db.collection("config").document("m365_auth").get()
                if auth_doc.exists:
                    calendar_id = auth_doc.to_dict().get("calendar_id")
            except Exception as exc:
                logging.exception("Failed to read calendar_id from config/m365_auth: %s", exc)
                calendar_id = None
                
        is_available = check_m365_availability(m365_token, m365_user_id, date_str, time_str, calendar_id)
        if not is_available:
            return ({
                "status": "error",
                "message": "No touring availability for that time. Please choose a different slot."
            }, 409, headers)

        # 3. Firestore Transaction — reserve slots
        transaction = db.transaction()
        booking_id = process_booking_transaction(
            transaction, inventory_ref, date_str, time_str, party_size, guest_data
        )

        try:
            # 4. QBO Invoice Generation
            qbo_token, realm_id = get_qbo_access_token()
            invoice_id, payment_link = create_qbo_invoice(qbo_token, realm_id, party_size, guest_data)

            # 5. M365 Calendar Event Injection
            event_guest_data = dict(guest_data)
            event_guest_data["party_size"] = party_size
            m365_event_id = inject_m365_event(
                m365_token, m365_user_id, date_str, time_str, event_guest_data, booking_id, calendar_id
            )

            # 6. Update booking document with integration IDs
            db.collection("bookings").document(booking_id).update({
                "integration_ids.qbo_invoice_id": invoice_id,
                "integration_ids.m365_event_id": m365_event_id,
                "payment_link": payment_link
            })

            token = db.collection('bookings').document(booking_id).get().to_dict().get('token')
            return ({
                "status": "success",
                "booking_id": str(booking_id),
                "payment_link": payment_link,
                "token": token
            }, 200, headers)

        except Exception as exc:
            logging.exception("Error during post-transaction integrations for booking %s: %s", booking_id, exc)
            # 7. Send immediate acknowledgment email via M365 about temporary issue (if any)
            try:
                m365_token, m365_user_id = get_m365_access_token()
                guest_email = guest_data.get("email")
                guest_name = guest_data.get("name", "Guest")
                # Using existing reminder function to send a temporary issue email
                send_outlook_reminder(
                    m365_token,
                    m365_user_id,
                    guest_email,
                    guest_name,
                    f"Temporary issue with your booking for {date_str} {time_str}. We will retry and notify you soon.",
                    booking_id,
                    payment_link=None,
                    party_size=party_size,
                )
            except Exception as email_err:
                logging.exception("Failed to send acknowledgment email for booking %s: %s", booking_id, email_err)

            # Attempt to rollback the partially created booking and inventory reservation
            try:
                # Delete booking document if it exists
                try:
                    db.collection("bookings").document(booking_id).delete()
                except Exception as del_err:
                    logging.exception("Failed to delete booking %s during rollback: %s", booking_id, del_err)

                # Revert inventory slot taken count if possible
                try:
                    inv_snap = inventory_ref.get()
                    if getattr(inv_snap, 'exists', True):
                        inv_data = inv_snap.to_dict() or {}
                        slots_data = inv_data.get("slots", {})
                        if isinstance(slots_data, dict) and time_str in slots_data:
                            slots_data[time_str]["taken"] = max(0, slots_data[time_str].get("taken", 0) - party_size)
                            inventory_ref.set({"slots": slots_data}, merge=True)
                except Exception as inv_err:
                    logging.exception("Failed to revert inventory for %s %s: %s", date_str, time_str, inv_err)
            except Exception as rb_err:
                logging.exception("Rollback encountered error for booking %s: %s", booking_id, rb_err)

            return ({"status": "error", "message": "Failed to process payload."}, 500, headers)

    except ValueError as e:
        return ({"status": "error", "message": str(e)}, 409, headers)
    except Exception as exc:
        logging.exception("Unhandled error in handle_booking: %s", exc)
        return ({"status": "error", "message": "Failed to process payload."}, 500, headers)


# ---------------------------------------------------------------------------
# QBO OAuth Endpoints
# ---------------------------------------------------------------------------

@functions_framework.http
def qbo_login(request):
    """
    Initiates the QuickBooks Online OAuth 2.0 flow.
    """
    auth_doc = {}
    if db.__class__.__name__ != 'DummyFirestore':
        try:
            auth_doc = db.collection("config").document("qbo_auth").get().to_dict() or {}
        except Exception:
            pass
    client_id, _, _, redirect_uri = _resolve_qbo_credentials(auth_doc)

    state = secrets.token_urlsafe(32)

    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'com.intuit.quickbooks.accounting',
        'state': state,
    }

    auth_url = 'https://appcenter.intuit.com/connect/oauth2?' + urlencode(params)
    resp = redirect(auth_url, code=302)
    resp.set_cookie('qbo_oauth_state', state, httponly=True, secure=True, max_age=600)
    return resp


@functions_framework.http
def qbo_callback(request):
    """
    Handles the callback from QuickBooks Online OAuth 2.0 flow.
    """
    code = request.args.get('code')
    realm_id = request.args.get('realmId')
    state = request.args.get('state')

    expected_state = request.cookies.get('qbo_oauth_state')
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        return ({"status": "error", "message": "Invalid state parameter"}, 400)

    if not code:
        return ({"status": "error", "message": "Missing authorization code."}, 400)

    auth_doc = {}
    if db.__class__.__name__ != 'DummyFirestore':
        try:
            auth_doc = db.collection("config").document("qbo_auth").get().to_dict() or {}
        except Exception:
            pass
    client_id, client_secret, _, redirect_uri = _resolve_qbo_credentials(auth_doc)

    token_endpoint = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'

    auth_str = f"{client_id}:{client_secret}"
    b64_auth_str = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')

    headers = {
        'Authorization': f'Basic {b64_auth_str}',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json'
    }

    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri
    }

    try:
        response = requests.post(token_endpoint, headers=headers, data=data, timeout=10)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get('access_token')
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in', 3600)

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        db.collection('config').document('qbo_auth').set({
            'access_token': access_token,
            'refresh_token': refresh_token,
            'realmId': realm_id,
            'expires_at': expires_at,
            'updated_at': firestore.SERVER_TIMESTAMP
        }, merge=True)

        return ({"status": "success", "message": "QBO Authentication successful."}, 200)

    except requests.exceptions.RequestException as e:
        return ({"status": "error", "message": f"Failed to exchange token: {str(e)}"}, 500)
    except Exception as e:
        return ({"status": "error", "message": f"An error occurred: {str(e)}"}, 500)


# ---------------------------------------------------------------------------
# M365 OAuth Endpoints  (mirrors QBO flow — one-time consent to get refresh_token)
# ---------------------------------------------------------------------------

@functions_framework.http
def m365_login(request):
    """
    Initiates the Microsoft 365 OAuth 2.0 delegated auth flow.
    Scopes requested: Calendars.ReadWrite, Mail.Send, offline_access.
    """
    auth_doc = db.collection("config").document("m365_auth").get().to_dict() or {}
    tenant_id = auth_doc.get("tenant_id", "common")
    client_id = auth_doc.get("client_id") or os.environ.get("M365_CLIENT_ID")
    redirect_uri = auth_doc.get("callback_url") or auth_doc.get("redirect_uri") or os.environ.get(
        "M365_REDIRECT_URI",
        "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-callback"
    )

    # Validate critical parameters
    if not client_id:
        return ({"status": "error", "message": "M365 client_id is not configured."}, 500)
    if not redirect_uri:
        return ({"status": "error", "message": "M365 redirect_uri is not configured."}, 500)

    state = secrets.token_urlsafe(32)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": "Calendars.ReadWrite Mail.Send offline_access",
        "state": state,
    }

    auth_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
        + urlencode(params)
    )
    resp = redirect(auth_url, code=302)
    resp.set_cookie("m365_oauth_state", state, httponly=True, secure=True, max_age=600)
    return resp


@functions_framework.http
def m365_callback(request):
    """
    Handles the callback from the Microsoft 365 OAuth 2.0 flow.
    Exchanges the auth code for tokens and persists them in config/m365_auth.
    """
    code = request.args.get("code")
    state = request.args.get("state")
    expected_state = request.cookies.get("m365_oauth_state")

    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        return ({"status": "error", "message": "Invalid state parameter"}, 400)

    if not code:
        return ({"status": "error", "message": "Missing authorization code."}, 400)

    auth_doc = db.collection("config").document("m365_auth").get().to_dict() or {}
    tenant_id = auth_doc.get("tenant_id", "common")
    client_id = auth_doc.get("client_id") or os.environ.get("M365_CLIENT_ID")
    client_secret = auth_doc.get("client_secret") or os.environ.get("M365_CLIENT_SECRET")
    redirect_uri = auth_doc.get("callback_url") or auth_doc.get("redirect_uri") or os.environ.get(
        "M365_REDIRECT_URI",
        "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-callback"
    )

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(token_url, data=data, timeout=10)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

        db.collection("config").document("m365_auth").update({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })

        return ({"status": "success", "message": "M365 authentication successful."}, 200)

    except requests.exceptions.RequestException as e:
        return ({"status": "error", "message": f"Failed to exchange M365 token: {str(e)}"}, 500)
    except Exception as e:
        return ({"status": "error", "message": f"An error occurred: {str(e)}"}, 500)


@functions_framework.http
def qbo_webhook(request):
    """
    Handles QuickBooks Online webhook events to update payment status to PAID.
    """
    if request.method != 'POST':
        return ('Method Not Allowed', 405)

    # 1. Retrieve verifier_token from Firestore config
    auth_doc_ref = db.collection("config").document("qbo_auth")
    auth_doc = auth_doc_ref.get()
    verifier_token = None
    if auth_doc.exists:
        _, _, verifier_token, _ = _resolve_qbo_credentials(auth_doc.to_dict())

    if not verifier_token or not isinstance(verifier_token, (str, bytes, int)):
        return ('Unauthorized: Missing or invalid verifier_token in configuration', 401)

    # 2. Check for Intuit-Signature header
    signature_header = request.headers.get("Intuit-Signature")
    if not signature_header:
        return ('Unauthorized: Missing Intuit-Signature header', 401)
    
    # 3. Base64 decode signature_header
    try:
        decoded_signature = base64.b64decode(signature_header)
    except Exception:
        return ('Unauthorized: Invalid Intuit-Signature encoding', 401)
    
    # 4. Compute HMAC-SHA256 signature of raw request payload
    payload = request.get_data()
    
    # Convert verifier token to bytes safely
    if isinstance(verifier_token, str):
        key_bytes = verifier_token.encode("utf-8")
    elif isinstance(verifier_token, bytes):
        key_bytes = verifier_token
    else:
        key_bytes = str(verifier_token).encode("utf-8")

    computed_signature = hmac.new(
        key_bytes,
        payload,
        hashlib.sha256
    ).digest()
    
    # 5. Use hmac.compare_digest to verify
    if not hmac.compare_digest(computed_signature, decoded_signature):
        return ('Unauthorized: Invalid Intuit-Signature', 401)

    try:
        event_data = request.get_json(silent=True)
        if not event_data:
            return ('Bad Request', 400)

        notifications = event_data.get("eventNotifications", [])
        for notification in notifications:
            entities = notification.get("dataChangeEvent", {}).get("entities", [])
            for entity in entities:
                if entity.get("name") == "Invoice" and entity.get("operation") in ("Update", "Create"):
                    invoice_id = entity.get("id")
                    if invoice_id:
                        # Query Firestore bookings for this invoice ID
                        bookings_ref = db.collection("bookings")
                        query = bookings_ref.where(
                            filter=firestore.FieldFilter("integration_ids.qbo_invoice_id", "==", invoice_id)
                        ).stream()
                        
                        for doc in query:
                            # Update payment status to PAID
                            doc.reference.update({"payment_status": "PAID"})

        return ({"status": "success"}, 200)

    except Exception as e:
        return ({"status": "error", "message": str(e)}, 500)

@functions_framework.http
def m365_free_availability(request):
    """Return available tour slots for each day as timestamps.
    Uses a single Microsoft Graph call per day to fetch all calendar events and
    compares them with already booked slots stored in Firestore. This reduces the
    number of Graph API requests dramatically compared to checking each hour
    individually.
    """
    if request.method != 'GET':
        return ('Method Not Allowed', 405)

    token, user_id = get_m365_access_token()
    # Resolve optional calendar_id configuration
    calendar_id = None
    if db.__class__.__name__ != 'DummyFirestore':
        try:
            auth_doc = db.collection("config").document("m365_auth").get()
            if auth_doc.exists:
                calendar_id = auth_doc.to_dict().get("calendar_id")
        except Exception:
            pass

    # Parse optional date range
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    today = datetime.now().date()
    start_date = datetime.strptime(start_str, '%Y-%m-%d').date() if start_str else today
    end_date = datetime.strptime(end_str, '%Y-%m-%d').date() if end_str else today + timedelta(days=30)

    # Typical tour hours (9:00‑16:00)


    result = {"dates": {}}
    current = start_date
    while current <= end_date:
        date_iso = current.isoformat()
        # ---------- Firestore booked slots ----------
        booked_hours = set()
        try:
            inventory_doc = db.collection("public").document(date_iso).get()
            if inventory_doc.exists:
                inventory = inventory_doc.to_dict() or {}
                slots = inventory.get("slots", [])
                for ts in slots:
                    # ts may be a Firestore Timestamp or datetime
                    if hasattr(ts, "to_datetime"):
                        dt = ts.to_datetime()
                    else:
                        dt = ts
                    if isinstance(dt, datetime):
                        booked_hours.add(dt.strftime("%H:%M"))
        except Exception:
            pass

        # ---------- Microsoft Graph events for the whole day ----------
        # Build start/end ISO strings for the day in Pacific time
        local_tz = ZoneInfo("America/Los_Angeles")
        day_start = datetime.combine(current, datetime.min.time()).replace(tzinfo=local_tz)
        day_end = datetime.combine(current, datetime.max.time()).replace(tzinfo=local_tz)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if calendar_id:
            url = (
                f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/calendarView"
                f"?startDateTime={day_start.isoformat()}&endDateTime={day_end.isoformat()}&$select=subject,showAs,start,end"
            )
        else:
            url = (
                f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView"
                f"?startDateTime={day_start.isoformat()}&endDateTime={day_end.isoformat()}&$select=subject,showAs,start,end"
            )
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            events = resp.json().get("value", [])
        except Exception:
            events = []

        # Determine which hours have a free "Touring Hours" block
        free_hours = set()
        for ev in events:
            subject = ev.get("subject", "")
            show_as = ev.get("showAs", "").lower()
            if subject.startswith(TOURING_HOURS_SUBJECT_PREFIX) and show_as in ("free", "tentative"):
                ev_start = datetime.fromisoformat(ev["start"]["dateTime"]).replace(tzinfo=ZoneInfo(ev["start"].get("timeZone", "UTC")))
                ev_end = datetime.fromisoformat(ev["end"]["dateTime"]).replace(tzinfo=ZoneInfo(ev["end"].get("timeZone", "UTC")))
                # iterate each hour within the event window
                hour_cursor = ev_start
                while hour_cursor < ev_end:
                    hour_str = hour_cursor.strftime("%H:%M")
                    free_hours.add(hour_str)
                    hour_cursor += timedelta(hours=1)
        # ---------- Build result slots ----------
        slots = []
        for hour in sorted(free_hours):
            if hour not in booked_hours:
                dt = datetime.combine(current, datetime.strptime(hour, "%H:%M").time()).replace(tzinfo=local_tz)
                slots.append(dt.isoformat())
        if slots:
            result["dates"][date_iso] = {"slots": slots}
        current += timedelta(days=1)
    return (jsonify(result), 200)
@functions_framework.http
def cancel_tour(request):
    """Customer cancels a tour using stored token."""
    # CORS setup (reuse same as handle_booking)
    origin = request.headers.get('Origin')
    allowed_origins = [
        "https://bodiefoundation.org",
        "https://www.bodiefoundation.org"
    ]
    cors_origin = "https://www.bodiefoundation.org"
    if origin:
        origin_lower = origin.lower()
        if (
            origin_lower in allowed_origins
            or origin_lower.endswith('.squarespace.com')
            or "localhost" in origin_lower
            or "127.0.0.1" in origin_lower
        ):
            cors_origin = origin
    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': cors_origin,
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)
    headers = {'Access-Control-Allow-Origin': cors_origin}
    try:
        # Parse parameters from URL query arguments
        booking_id = request.args.get('booking_id')
        token = request.args.get('token')
        if not booking_id or not token:
            return ({"status": "error", "message": "Missing booking_id or token"}, 400, headers)
        booking_ref = db.collection('bookings').document(booking_id)
        booking_doc = booking_ref.get()
        if not booking_doc.exists:
            return ({"status": "error", "message": "Booking not found"}, 404, headers)
        data = booking_doc.to_dict()
        if data.get('token') != token:
            return ({"status": "error", "message": "Invalid token"}, 403, headers)
        # Release the slot
        tour_dt = data.get('tour_datetime')
        if tour_dt:
            date_str = tour_dt.strftime('%Y-%m-%d')
            inventory_ref = db.collection('public').document(date_str)
            try:
                inventory_ref.update({"slots": firestore.ArrayRemove([tour_dt])})
            except Exception:
                pass
        # Update payment_status instead of deleting the booking
        booking_ref.update({"payment_status": "CANCELLED_BY_GUEST"})
        return ({"status": "success", "message": "Booking cancelled"}, 200, headers)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ({"status": "error", "message": str(e)}, 500, headers)
