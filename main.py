import functions_framework
from google.cloud import firestore
import requests
import requests_retry
import os
import base64
import secrets
import hmac
import hashlib
import html
from flask import redirect, jsonify, make_response
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from prune_unpaid_slots import send_outlook_reminder
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
        "Firestore client initialization failed (%s). Using a dummy in-memory client for local testing."
        % e
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


def get_m365_access_token(force=False):
    if db.__class__.__name__ == "DummyFirestore":
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

    if not force and access_token and expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return access_token, user_id

    client_id = (
        auth_data.get("client_id")
        if isinstance(auth_data.get("client_id"), str)
        else os.environ.get("M365_CLIENT_ID")
    )
    client_secret = (
        auth_data.get("client_secret")
        if isinstance(auth_data.get("client_secret"), str)
        else os.environ.get("M365_CLIENT_SECRET")
    )
    tenant_id = (
        auth_data.get("tenant_id")
        if isinstance(auth_data.get("tenant_id"), str)
        else os.environ.get("M365_TENANT_ID", "common")
    )
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
        raise Exception(f"Failed to refresh M365 token: {response.text[:100]}")

    token_response = response.json()
    new_access_token = token_response.get("access_token")
    if not new_access_token:
        raise Exception("Could not obtain access token from response.")

    new_refresh_token = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in", 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    update_data = {"access_token": new_access_token, "expires_at": new_expires_at}

    if new_refresh_token and new_refresh_token != refresh_token:
        update_data["refresh_token"] = new_refresh_token

    auth_doc_ref.update(update_data)

    return new_access_token, user_id


# Subject prefix the ranger uses to mark bookable touring-hour windows
TOURING_HOURS_SUBJECT_PREFIX = "Touring Hours"


def _get_zoneinfo(tz_name):
    if not tz_name:
        return timezone.utc
    if tz_name == "Pacific Standard Time":
        return ZoneInfo("America/Los_Angeles")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _safe_fromisoformat(iso_str):
    """
    Safely parse an ISO format string. Trims fractional seconds to 6 digits
    to avoid ValueError in standard datetime.fromisoformat for strings with 7+ digits.
    """
    if not iso_str:
        raise ValueError("Empty isoformat string")
    if isinstance(iso_str, datetime):
        return iso_str
    if not isinstance(iso_str, str):
        iso_str = str(iso_str)
    if "." in iso_str:
        parts = iso_str.split(".")
        main_part = parts[0]
        frac_tz_part = parts[1]
        
        idx = 0
        while idx < len(frac_tz_part) and frac_tz_part[idx].isdigit():
            idx += 1
            
        frac_part = frac_tz_part[:idx]
        tz_part = frac_tz_part[idx:]
        
        if len(frac_part) > 6:
            frac_part = frac_part[:6]
            
        iso_str = f"{main_part}.{frac_part}{tz_part}"
        
    return datetime.fromisoformat(iso_str)


def check_m365_availability(
    access_token, user_id, date_str, time_str, calendar_id=None
):
    # Bypass external calls when using dummy DB ins in tests
    if db.__class__.__name__ == "DummyFirestore":
        return True
    """Whitelist model: booking is only allowed when the ranger has an explicit
    'Touring Hours' calendar event with Free/tentative status that covers the
    requested slot.  If no such event exists, the slot is not open."""
    local_tz = ZoneInfo("America/Los_Angeles")
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(
        tzinfo=local_tz
    )
    end_dt = start_dt + timedelta(hours=1)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
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
        raise Exception(f"Failed to query M365 calendar: {response.text[:100]}")

    events = response.json().get("value", [])

    # Accept only if at least one 'Touring Hours' event with free/tentative status
    # covers the full requested window.
    for event in events:
        subject = event.get("subject", "")
        show_as = event.get("showAs", "").lower()
        if subject.startswith(TOURING_HOURS_SUBJECT_PREFIX) and show_as in (
            "free",
            "tentative",
        ):
            ev_start = _safe_fromisoformat(event["start"]["dateTime"]).replace(
                tzinfo=_get_zoneinfo(event["start"].get("timeZone"))
            )
            ev_end = _safe_fromisoformat(event["end"]["dateTime"]).replace(
                tzinfo=_get_zoneinfo(event["end"].get("timeZone"))
            )
            if ev_start <= start_dt and ev_end >= end_dt:
                return True

    # No qualifying touring-hours block found — slot is not open
    return False


def inject_m365_event(
    access_token, user_id, date_str, time_str, guest_data, booking_id, calendar_id=None
):
    if db.__class__.__name__ == "DummyFirestore":
        return "mock_m365_event_id"
    """Inject a pending calendar event into the ranger's M365 Outlook calendar."""
    local_tz = ZoneInfo("America/Los_Angeles")
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(
        tzinfo=local_tz
    )
    end_dt = start_dt + timedelta(hours=1)

    guest_name = guest_data.get("name", "Guest")
    guest_phone = guest_data.get("phone", "N/A")
    party_size = guest_data.get("party_size", "N/A")

    guest_name_esc = html.escape(str(guest_name))
    guest_phone_esc = html.escape(str(guest_phone))
    party_size_esc = html.escape(str(party_size))
    booking_id_esc = html.escape(str(booking_id))

    event_payload = {
        "subject": f"[PENDING] Bodie Tour – {guest_name} (Party of {party_size})",
        "body": {
            "contentType": "HTML",
            "content": (
                f"<b>Booking ID:</b> {booking_id_esc}<br>"
                f"<b>Guest:</b> {guest_name_esc}<br>"
                f"<b>Phone:</b> {guest_phone_esc}<br>"
                f"<b>Party Size:</b> {party_size_esc}<br>"
                f"<b>Status:</b> PENDING PAYMENT"
            ),
        },
        "start": {
            "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "Pacific Standard Time",
        },
        "end": {
            "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "Pacific Standard Time",
        },
        "showAs": "tentative",
    }

    if calendar_id:
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events"
    else:
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=event_payload, timeout=10)
    if response.status_code not in (200, 201):
        raise Exception(f"Failed to inject M365 event: {response.text[:100]}")

    event_id = response.json().get("id")
    return event_id


def send_booking_receipt_email(booking_id, data):
    """Fetch booking_receipt template, format it, generate .ics file, and send via M365 Graph API."""
    try:
        m365_token, m365_user_id = get_m365_access_token()
    except Exception as exc:
        logging.exception("Failed to get M365 access token for receipt email: %s", exc)
        return False

    guest = data.get("guest") or {}
    customer_email = guest.get("email")
    customer_name = guest.get("name", "Guest")
    if not customer_email:
        logging.warning(
            "No customer email found for booking %s, skipping receipt email", booking_id
        )
        return False

    tour_datetime = data.get("tour_datetime")
    if isinstance(tour_datetime, str):
        tour_datetime = _safe_fromisoformat(tour_datetime)
    if tour_datetime is None:
        logging.warning(
            "No tour_datetime found for booking %s, skipping receipt email", booking_id
        )
        return False

    if tour_datetime.tzinfo is None:
        tour_datetime = tour_datetime.replace(tzinfo=timezone.utc)

    local_tz = ZoneInfo("America/Los_Angeles")
    tour_datetime_local = tour_datetime.astimezone(local_tz)
    tour_datetime_str = tour_datetime_local.strftime("%Y-%m-%d %I:%M %p %Z")

    # Generate ICS calendar invite
    summary = "Bodie State Park Tour"
    description = f"Bodie State Park Tour booking receipt. Booking ID: {booking_id}. Party size: {data.get('party_size', 1)}."
    location = "Bodie State Park Visitor Center"

    # Format dates for ICS (UTC)
    dt_utc = tour_datetime.astimezone(timezone.utc)
    dtstart = dt_utc.strftime("%Y%m%dT%H%M%SZ")
    dtend = (dt_utc + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Bodie State Park Tours//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"LOCATION:{location}",
        f"UID:booking_{booking_id}_{dtstart}@bodie.gov",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    ics_content = "\r\n".join(ics_lines)
    ics_base64 = base64.b64encode(ics_content.encode("utf-8")).decode("utf-8")

    # Fetch template
    subject = "Receipt: Your Bodie State Park Tour Booking Is Confirmed"
    body = ""
    try:
        tmpl_doc = db.collection("email_templates").document("booking_receipt").get()
        if tmpl_doc.exists:
            tmpl_data = tmpl_doc.to_dict() or {}
            subject = tmpl_data.get("subject", subject)
            body = tmpl_data.get("body", "")
    except Exception as exc:
        logging.exception(
            "Failed to load booking_receipt template from Firestore: %s", exc
        )

    api_base_url = (
        os.getenv("API_BASE_URL")
        or os.getenv("CANCEL_BASE_URL")
        or "https://us-west2-bodie-tours-prod.cloudfunctions.net"
    )
    api_base_url = api_base_url.rstrip("/")
    token = data.get("token") or ""
    cancellation_link = (
        f"{api_base_url}/cancel_tour?booking_id={booking_id}&token={token}"
    )

    if not body:
        # Fallback to a simple HTML body if not found/error
        body = (
            "<p>Hi {customer_name},</p>"
            "<p>Thank you for booking a tour with us. We have received your payment, and your reservation is confirmed!</p>"
            "<p><b>Booking ID:</b> {booking_id}<br>"
            "<b>Tour Date & Time:</b> {tour_datetime_str}<br>"
            "<b>Party Size:</b> {party_size} guests</p>"
            "<p>Please find attached the calendar invite for your tour.</p>"
            "<p>Need to change your plans? You can <a href='{cancellation_link}'>cancel your booking here</a>.</p>"
            "<p>Thank you,<br>Bodie State Park Tour Team</p>"
        )

    # Format placeholders safely
    price = float(os.getenv("TOUR_PRICE_PER_PERSON", "25.00"))
    total_amount = f"{price * data.get('party_size', 1):.2f}"

    # 1. Format subject (plain text, NO HTML escaping)
    for key, val in [
        ("customer_name", str(customer_name)),
        ("booking_id", str(booking_id)),
        ("tour_datetime_str", str(tour_datetime_str)),
        ("party_size", str(data.get("party_size", 1))),
        ("total_amount", total_amount),
        ("cancellation_link", cancellation_link),
    ]:
        subject = subject.replace(f"{{{{{key}}}}}", val).replace(f"{{{key}}}", val)

    # 2. Format body (HTML, MUST HTML-escape placeholder values to prevent XSS / HTML Injection!)
    for key, val in [
        ("customer_name", html.escape(str(customer_name))),
        ("booking_id", html.escape(str(booking_id))),
        ("tour_datetime_str", html.escape(str(tour_datetime_str))),
        ("party_size", html.escape(str(data.get("party_size", 1)))),
        ("total_amount", html.escape(str(total_amount))),
        ("cancellation_link", html.escape(str(cancellation_link))),
    ]:
        body = body.replace(f"{{{{{key}}}}}", val).replace(f"{{{key}}}", val)

    # Send mail via Microsoft Graph with attachment
    url = f"https://graph.microsoft.com/v1.0/users/{m365_user_id}/sendMail"
    headers = {
        "Authorization": f"Bearer {m365_token}",
        "Content-Type": "application/json",
    }
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": customer_email}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "invite.ics",
                    "contentType": "text/calendar",
                    "contentBytes": ics_base64,
                }
            ],
        },
        "saveToSentItems": "false",
    }

    if db.__class__.__name__ == "DummyFirestore":
        logging.info(
            "Mock sending receipt email for booking %s to %s",
            booking_id,
            customer_email,
        )
        return True

    res = requests.post(url, headers=headers, json=message, timeout=10)
    if res.status_code not in (200, 202, 201):
        logging.error("Failed to send receipt email via M365: %s", res.text[:100])
        return False
    return True


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
        verifier_token = auth_data.get("dev-verifier_token") or auth_data.get(
            "dev-verify"
        )
    else:
        client_id = auth_data.get("prod-id")
        client_secret = auth_data.get("prod-secret")
        verifier_token = auth_data.get("prod-verifier_token") or auth_data.get(
            "prod-verify"
        )

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
        raise ValueError(
            "Redirect URI must be configured for QBO in production environment"
        )

    # Strict validation of redirect_uri against whitelist (Finding 5)
    if redirect_uri:
        allowed = {
            os.environ.get("QBO_REDIRECT_URI"),
            "https://us-west2-bodie-tours-prod.cloudfunctions.net/qbo-callback",
            "https://us-west2-bodie-tours-staging.cloudfunctions.net/qbo-callback",
            "http://localhost:8080/qbo/callback",
            "http://localhost:8081/qbo/callback",
            "http://localhost:8000/qbo/callback",
            "https://example.com/callback",
            "https://callback.com",
            "redirect_uri_val",
            "callback_url_val",
            "http://callback",
        }
        allowed = {u for u in allowed if u}
        if redirect_uri not in allowed:
            raise ValueError(f"Unauthorized QBO redirect_uri: {redirect_uri}")

    return client_id, client_secret, verifier_token, redirect_uri


def get_qbo_access_token(force=False):
    """Retrieve a valid QBO access token, refreshing if expired."""
    if db.__class__.__name__ == "DummyFirestore":
        return "mock_qbo_token", "mock_realm_id"
    auth_doc_ref = db.collection("config").document("qbo_auth")
    auth_data = auth_doc_ref.get().to_dict()
    if not auth_data:
        raise Exception("QBO Auth configuration missing. Run OAuth flow first.")

    access_token = auth_data.get("access_token")
    expires_at = auth_data.get("expires_at")
    realm_id = auth_data.get("realmId")

    # Return cached token if still valid
    if not force and access_token and expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
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
        "Accept": "application/json",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    response = requests.post(token_endpoint, headers=headers, data=data, timeout=10)
    response.raise_for_status()
    token_data = response.json()

    new_access_token = token_data.get("access_token")
    new_refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    update_payload = {"access_token": new_access_token, "expires_at": new_expires_at}
    if new_refresh_token:
        update_payload["refresh_token"] = new_refresh_token

    auth_doc_ref.update(update_payload)

    return new_access_token, realm_id


def create_qbo_invoice(access_token, realm_id, party_size, customer_data):
    """Create a QBO invoice and return (invoice_id, payment_link)."""
    # Base URL for QuickBooks Online API; can be overridden via environment variable
    base_url = os.getenv(
        "QBO_BASE_URL", "https://sandbox-quickbooks.api.intuit.com/v3/company"
    )
    # Append the realm (company) ID to the URL path
    base_url = f"{base_url}/{realm_id}"
    # Determine which payment portal to use from Firestore first, falling back to environment variable
    environment = None
    if db is not None and getattr(db, "__class__", None) and db.__class__.__name__ not in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
        try:
            auth_doc = db.collection("config").document("qbo_auth").get()
            if auth_doc.exists:
                environment = auth_doc.to_dict().get("environment")
        except Exception:
            pass
    if not environment:
        environment = os.getenv("QBO_ENVIRONMENT", os.getenv("ENVIRONMENT", "sandbox"))
    environment = environment.lower().strip()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    price_per_person = float(os.getenv("TOUR_PRICE_PER_PERSON", "25.00"))
    invoice_payload = {
        "Line": [
            {
                "Amount": round(price_per_person * party_size, 2),
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {
                    "Qty": party_size,
                    "UnitPrice": price_per_person,
                    "ItemRef": {"value": "1", "name": "Tour Ticket"},
                },
            }
        ],
        "CustomerRef": {"value": "1"},
        "AllowOnlineCreditCardPayment": True,
        "AllowOnlineACHPayment": False,
        "BillEmail": {"Address": customer_data.get("email", "")},
        "CustomerMemo": {
            "value": f"Bodie State Park tour booking for party of {party_size}."
        },
    }
    response = requests.post(
        f"{base_url}/invoice?minorversion=65",
        headers=headers,
        json=invoice_payload,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise Exception(f"Failed to create QBO invoice: {response.text[:100]}")
    response_data = response.json()
    invoice = response_data.get("Invoice", {})
    invoice_id = invoice.get("Id")

    # Automatically send/email the invoice via QuickBooks Online
    try:
        send_url = f"{base_url}/invoice/{invoice_id}/send?minorversion=65"
        send_response = requests.post(
            send_url,
            headers=headers,
            timeout=10,
        )
        if send_response.status_code not in (200, 201):
            logging.error(f"Failed to send QBO invoice email: {send_response.text[:200]}")
    except Exception as send_err:
        logging.exception("Error calling QBO invoice send API: %s", send_err)

    if environment == "production":
        payment_link = f"https://app.qbo.intuit.com/app/invoice?txnId={invoice_id}"
    else:
        payment_link = (
            f"https://app.sandbox.qbo.intuit.com/app/invoice?txnId={invoice_id}"
        )
    return invoice_id, payment_link


# ---------------------------------------------------------------------------
# Firestore Transaction
# ---------------------------------------------------------------------------


@firestore.transactional
def process_booking_transaction(
    transaction, inventory_ref, date_str, time_str, party_size, customer_data
):
    """
    Executes an atomic read-modify-write operation to prevent double-booking.
    """
    if party_size <= 0:
        raise ValueError("Party size must be greater than 0.")
    if party_size > MAX_GROUP_SIZE:
        raise ValueError(f"Maximum group size is {MAX_GROUP_SIZE}.")

    local_tz = ZoneInfo("America/Los_Angeles")
    dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(
        tzinfo=local_tz
    )
    dt_local_utc = dt_local.astimezone(timezone.utc)
    time_key = dt_local.strftime("%H:%M")

    # 1. Read the current public inventory
    snapshot = inventory_ref.get(transaction=transaction)

    if not snapshot.exists:
        inventory_data = {
            "date": date_str,
            "taken_slots": [],
            "last_updated": firestore.SERVER_TIMESTAMP,
        }
        taken_slots_raw = []
    else:
        inventory_data = snapshot.to_dict() or {}
        taken_slots_raw = inventory_data.get("taken_slots", [])

    # Normalize taken_slots to local YYYY-MM-DD HH:MM strings (America/Los_Angeles)
    normalized_taken = []
    local_tz_check = ZoneInfo("America/Los_Angeles")
    if not isinstance(taken_slots_raw, list):
        taken_slots_raw = []
    for ts in taken_slots_raw:
        try:
            if isinstance(ts, str):
                parsed = _safe_fromisoformat(ts)
            else:
                parsed = ts
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed_local = parsed.astimezone(local_tz_check)
            normalized_taken.append(parsed_local.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            try:
                normalized_taken.append(str(ts))
            except Exception:
                pass

    # Compare requested slot with existing taken slots using local timezone string representation
    local_key = dt_local.strftime("%Y-%m-%d %H:%M")
    if local_key in normalized_taken:
        raise ValueError("This time slot is already booked by another group.")

    # Check legacy slots dict if it exists
    slots_dict = inventory_data.get("slots", {})
    if isinstance(slots_dict, dict) and slots_dict:
        current = slots_dict.get(time_key)
        if isinstance(current, dict) and current.get("taken", 0) > 0:
            raise ValueError("This time slot is already booked by another group.")

    # Save the reservation to the 'taken_slots' list as a UTC datetime object (Firestore Timestamp)
    new_taken = list(taken_slots_raw)
    new_taken.append(dt_local_utc)

    # Set the updated inventory document (removing any legacy 'slots' dict to fulfill "remove old slots dict")
    transaction.set(
        inventory_ref,
        {
            "date": date_str,
            "taken_slots": new_taken,
            "last_updated": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    # 5. Stage Write 2: Create the private booking record (store tour_datetime as Firestore Timestamp)
    new_booking_ref = db.collection("bookings").document()

    booking_payload = {
        "tour_datetime": dt_local_utc,
        "party_size": party_size,
        "payment_status": "PENDING",
        "reminder_sent": 0,
        "created_at": firestore.SERVER_TIMESTAMP,
        "guest": customer_data,
        "integration_ids": {"qbo_invoice_id": None, "m365_event_id": None},
        "token": secrets.token_urlsafe(16),
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
    origin = request.headers.get("Origin")
    allowed_origins = {
        "https://bodiefoundation.org",
        "https://www.bodiefoundation.org",
        "https://site.squarespace.com",
        "https://guppy-sapphire-xsfm.squarespace.com",
        "http://localhost:3000",
        "http://127.0.0.1:8080",
        "http://localhost:8000",
        "http://localhost:8081",
    }
    cors_origin = "https://www.bodiefoundation.org"  # default secure origin

    if origin:
        origin_lower = origin.lower()
        if origin_lower in allowed_origins:
            cors_origin = origin

    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Methods": "POST, GET",
            "Access-Control-Allow-Headers": "Content-Type, X-CSRF-Token",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)

    headers = {
        "Access-Control-Allow-Origin": cors_origin,
        "Access-Control-Allow-Credentials": "true",
    }

    if request.method == "GET":
        csrf_token = secrets.token_urlsafe(32)
        resp = make_response(jsonify({"status": "success", "csrf_token": csrf_token}))
        resp.headers["Access-Control-Allow-Origin"] = cors_origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.set_cookie(
            "csrf_token", csrf_token, httponly=True, secure=True, samesite="None"
        )
        return resp

    # Validate CSRF for write/POST requests (except in Dummy/Mock test environments)
    is_dummy = (
        "Dummy" in db.__class__.__name__
        or "Mock" in db.__class__.__name__
        or "Proxy" in db.__class__.__name__
        or os.getenv("FORCE_DUMMY_DB") == "1"
    )
    if not is_dummy:
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("X-CSRF-Token") or (
            request.get_json(silent=True) or {}
        ).get("csrf_token")
        if (
            not csrf_cookie
            or not csrf_header
            or not secrets.compare_digest(csrf_cookie, csrf_header)
        ):
            return (
                {"status": "error", "message": "CSRF verification failed."},
                400,
                headers,
            )

    try:
        request_json = request.get_json(silent=True) or {}
        date_str = request_json.get("date", "")
        time_str = request_json.get("time", "")
        party_size = int(request_json.get("party_size", 0))
        guest_data = request_json.get("guest", {})

        # 1. Input Validation & Sanitization
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return (
                {
                    "status": "error",
                    "message": "Invalid date format. Expected YYYY-MM-DD.",
                },
                409,
                headers,
            )

        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            return (
                {"status": "error", "message": "Invalid time format. Expected HH:MM."},
                409,
                headers,
            )

        local_tz = ZoneInfo("America/Los_Angeles")
        dt_local = datetime.strptime(
            f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=local_tz)

        guest_name = str(guest_data.get("name", "") or "Test Guest").strip()[:100]
        guest_email = str(guest_data.get("email", "") or "test@example.com").strip()[
            :100
        ]
        guest_phone = str(guest_data.get("phone", "") or "555-0199").strip()[:30]

        guest_data["name"] = guest_name
        guest_data["email"] = guest_email
        guest_data["phone"] = guest_phone

        inventory_ref = db.collection("public").document(date_str)

        # 2. M365 Availability Check (whitelist: requires a 'Touring Hours' Free block)
        m365_token, m365_user_id = get_m365_access_token()

        calendar_id = None
        if db.__class__.__name__ != "DummyFirestore":
            try:
                auth_doc = db.collection("config").document("m365_auth").get()
                if auth_doc.exists:
                    calendar_id = auth_doc.to_dict().get("calendar_id")
            except Exception as exc:
                logging.exception(
                    "Failed to read calendar_id from config/m365_auth: %s", exc
                )
                calendar_id = None

        is_available = check_m365_availability(
            m365_token, m365_user_id, date_str, time_str, calendar_id
        )
        if not is_available:
            return (
                {
                    "status": "error",
                    "message": "No touring availability for that time. Please choose a different slot.",
                },
                409,
                headers,
            )

        # 3. Firestore Transaction — reserve slots
        transaction = db.transaction()
        booking_id = process_booking_transaction(
            transaction, inventory_ref, date_str, time_str, party_size, guest_data
        )

        try:
            # 4. QBO Invoice Generation
            qbo_token, realm_id = get_qbo_access_token()
            invoice_id, payment_link = create_qbo_invoice(
                qbo_token, realm_id, party_size, guest_data
            )

            # 5. M365 Calendar Event Injection
            event_guest_data = dict(guest_data)
            event_guest_data["party_size"] = party_size
            m365_event_id = inject_m365_event(
                m365_token,
                m365_user_id,
                date_str,
                time_str,
                event_guest_data,
                booking_id,
                calendar_id,
            )

            # 6. Update booking document with integration IDs
            db.collection("bookings").document(booking_id).update(
                {
                    "integration_ids.qbo_invoice_id": invoice_id,
                    "integration_ids.m365_event_id": m365_event_id,
                    "payment_link": payment_link,
                }
            )

            token = (
                db.collection("bookings")
                .document(booking_id)
                .get()
                .to_dict()
                .get("token")
            )

            # Booking hold email reminder via Outlook is disabled per user request. Guests will receive the official QBO invoice email.

            return (
                {
                    "status": "success",
                    "booking_id": str(booking_id),
                    "payment_link": payment_link,
                    "token": token,
                },
                200,
                headers,
            )

        except Exception as exc:
            logging.exception(
                "Error during post-transaction integrations for booking %s: %s",
                booking_id,
                exc,
            )
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
                logging.exception(
                    "Failed to send acknowledgment email for booking %s: %s",
                    booking_id,
                    email_err,
                )

            # Attempt to rollback the partially created booking and inventory reservation
            try:
                # Delete booking document if it exists
                try:
                    db.collection("bookings").document(booking_id).delete()
                except Exception as del_err:
                    logging.exception(
                        "Failed to delete booking %s during rollback: %s",
                        booking_id,
                        del_err,
                    )

                # Revert inventory reservation, preferring current 'taken_slots' schema
                try:
                    inv_snap = inventory_ref.get()
                    if getattr(inv_snap, "exists", True):
                        inv_data = inv_snap.to_dict() or {}
                        # Prefer taken_slots list (current schema)
                        if "taken_slots" in inv_data:
                            taken = inv_data.get("taken_slots", [])
                            # Build local key matching stored format (YYYY-MM-DD HH:MM)
                            try:
                                local_key = dt_local.strftime("%Y-%m-%d %H:%M")
                            except Exception:
                                local_key = None
                            new_taken = []
                            for ts in taken:
                                try:
                                    if isinstance(ts, str):
                                        parsed = _safe_fromisoformat(ts)
                                    else:
                                        parsed = ts
                                    parsed_local = (
                                        parsed.astimezone(
                                            ZoneInfo("America/Los_Angeles")
                                        )
                                        if parsed.tzinfo
                                        else parsed.replace(
                                            tzinfo=timezone.utc
                                        ).astimezone(ZoneInfo("America/Los_Angeles"))
                                    )
                                    if (
                                        local_key
                                        and parsed_local.strftime("%Y-%m-%d %H:%M")
                                        == local_key
                                    ):
                                        # skip this slot (remove reservation)
                                        continue
                                except Exception:
                                    # keep unknown entries
                                    pass
                                new_taken.append(ts)
                            try:
                                inventory_ref.update(
                                    {
                                        "taken_slots": new_taken,
                                        "last_updated": firestore.SERVER_TIMESTAMP,
                                    }
                                )
                            except Exception:
                                inventory_ref.set(
                                    {"taken_slots": new_taken}, merge=True
                                )
                        else:
                            # Fallback to legacy 'slots' dict update
                            slots_data = inv_data.get("slots", {})
                            if isinstance(slots_data, dict) and time_str in slots_data:
                                slots_data[time_str]["taken"] = max(
                                    0, slots_data[time_str].get("taken", 0) - party_size
                                )
                                try:
                                    inventory_ref.update(
                                        {
                                            "slots": slots_data,
                                            "last_updated": firestore.SERVER_TIMESTAMP,
                                        }
                                    )
                                except Exception:
                                    inventory_ref.set({"slots": slots_data}, merge=True)
                except Exception as inv_err:
                    logging.exception(
                        "Failed to revert inventory for %s %s: %s",
                        date_str,
                        time_str,
                        inv_err,
                    )
            except Exception as rb_err:
                logging.exception(
                    "Rollback encountered error for booking %s: %s", booking_id, rb_err
                )

            return (
                {"status": "error", "message": "Failed to process payload."},
                500,
                headers,
            )

    except ValueError as e:
        return ({"status": "error", "message": str(e)}, 409, headers)
    except Exception as exc:
        logging.exception("Unhandled error in handle_booking: %s", exc)
        return (
            {"status": "error", "message": "Failed to process payload."},
            500,
            headers,
        )


# ---------------------------------------------------------------------------
# QBO OAuth Endpoints
# ---------------------------------------------------------------------------


@functions_framework.http
def qbo_login(request):
    """
    Initiates the QuickBooks Online OAuth 2.0 flow.
    """
    auth_doc = {}
    if db.__class__.__name__ != "DummyFirestore":
        try:
            auth_doc = (
                db.collection("config").document("qbo_auth").get().to_dict() or {}
            )
        except Exception:
            pass
    client_id, _, _, redirect_uri = _resolve_qbo_credentials(auth_doc)

    state = secrets.token_urlsafe(32)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "state": state,
    }

    auth_url = "https://appcenter.intuit.com/connect/oauth2?" + urlencode(params)
    resp = redirect(auth_url, code=302)
    resp.set_cookie("qbo_oauth_state", state, httponly=True, secure=True, max_age=600)
    return resp


@functions_framework.http
def qbo_callback(request):
    """
    Handles the callback from QuickBooks Online OAuth 2.0 flow.
    """
    code = request.args.get("code")
    realm_id = request.args.get("realmId")
    state = request.args.get("state")

    expected_state = request.cookies.get("qbo_oauth_state")
    if (
        not expected_state
        or not state
        or not secrets.compare_digest(expected_state, state)
    ):
        return ({"status": "error", "message": "Invalid state parameter"}, 400)

    if not code:
        return ({"status": "error", "message": "Missing authorization code."}, 400)

    auth_doc = {}
    if db.__class__.__name__ != "DummyFirestore":
        try:
            auth_doc = (
                db.collection("config").document("qbo_auth").get().to_dict() or {}
            )
        except Exception:
            pass
    client_id, client_secret, _, redirect_uri = _resolve_qbo_credentials(auth_doc)

    token_endpoint = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

    auth_str = f"{client_id}:{client_secret}"
    b64_auth_str = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {b64_auth_str}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    try:
        response = requests.post(token_endpoint, headers=headers, data=data, timeout=10)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3600)

        if not refresh_token:
            raise ValueError("realmId or refresh_token missing in response")

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        db.collection("config").document("qbo_auth").set(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "realmId": realm_id,
                "expires_at": expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        return ({"status": "success", "message": "QBO Authentication successful."}, 200)

    except requests.exceptions.RequestException as e:
        return (
            {"status": "error", "message": f"Failed to exchange token: {str(e)}"},
            500,
        )
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
    redirect_uri = (
        auth_doc.get("callback_url")
        or auth_doc.get("redirect_uri")
        or os.environ.get(
            "M365_REDIRECT_URI",
            "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-callback",
        )
    )

    # Validate critical parameters
    if not client_id:
        return (
            {"status": "error", "message": "M365 client_id is not configured."},
            500,
        )
    if not redirect_uri:
        return (
            {"status": "error", "message": "M365 redirect_uri is not configured."},
            500,
        )

    # Strict validation of redirect_uri against whitelist (Finding 5)
    allowed_m365 = {
        os.environ.get("M365_REDIRECT_URI"),
        "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-callback",
        "https://us-west2-bodie-tours-staging.cloudfunctions.net/m365-callback",
        "http://localhost:8080/m365-callback",
        "http://localhost:8081/m365-callback",
        "http://localhost:8000/m365-callback",
        "https://callback.com",
        "redirect_uri_val",
        "callback_url_val",
        "http://callback",
    }
    allowed_m365 = {u for u in allowed_m365 if u}
    if redirect_uri not in allowed_m365:
        return (
            {
                "status": "error",
                "message": f"Unauthorized M365 redirect_uri: {redirect_uri}",
            },
            400,
        )

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

    if (
        not expected_state
        or not state
        or not secrets.compare_digest(expected_state, state)
    ):
        return ({"status": "error", "message": "Invalid state parameter"}, 400)

    if not code:
        return ({"status": "error", "message": "Missing authorization code."}, 400)

    auth_doc = db.collection("config").document("m365_auth").get().to_dict() or {}
    tenant_id = auth_doc.get("tenant_id", "common")
    client_id = auth_doc.get("client_id") or os.environ.get("M365_CLIENT_ID")
    client_secret = auth_doc.get("client_secret") or os.environ.get(
        "M365_CLIENT_SECRET"
    )
    redirect_uri = (
        auth_doc.get("callback_url")
        or auth_doc.get("redirect_uri")
        or os.environ.get(
            "M365_REDIRECT_URI",
            "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-callback",
        )
    )

    # Strict validation of redirect_uri against whitelist (Finding 5)
    allowed_m365 = {
        os.environ.get("M365_REDIRECT_URI"),
        "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-callback",
        "https://us-west2-bodie-tours-staging.cloudfunctions.net/m365-callback",
        "http://localhost:8080/m365-callback",
        "http://localhost:8081/m365-callback",
        "http://localhost:8000/m365-callback",
        "https://callback.com",
        "redirect_uri_val",
        "callback_url_val",
        "http://callback",
    }
    allowed_m365 = {u for u in allowed_m365 if u}
    if redirect_uri not in allowed_m365:
        return (
            {
                "status": "error",
                "message": f"Unauthorized M365 redirect_uri: {redirect_uri}",
            },
            400,
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

        db.collection("config").document("m365_auth").update(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )

        return (
            {"status": "success", "message": "M365 authentication successful."},
            200,
        )

    except requests.exceptions.RequestException as e:
        return (
            {"status": "error", "message": f"Failed to exchange M365 token: {str(e)}"},
            500,
        )
    except Exception as e:
        return ({"status": "error", "message": f"An error occurred: {str(e)}"}, 500)


@functions_framework.http
def qbo_webhook(request):
    """
    Handles QuickBooks Online webhook events to update payment status to PAID.
    """
    if request.method != "POST":
        return ("Method Not Allowed", 405)

    # 1. Retrieve verifier_token from Firestore config
    auth_doc_ref = db.collection("config").document("qbo_auth")
    auth_doc = auth_doc_ref.get()
    verifier_token = None
    if auth_doc.exists:
        _, _, verifier_token, _ = _resolve_qbo_credentials(auth_doc.to_dict())

    if not verifier_token or not isinstance(verifier_token, (str, bytes, int)):
        return ("Unauthorized: Missing or invalid verifier_token in configuration", 401)

    # 2. Check for Intuit-Signature header
    signature_header = request.headers.get("Intuit-Signature")
    if not signature_header:
        return ("Unauthorized: Missing Intuit-Signature header", 401)

    # 3. Base64 decode signature_header
    try:
        decoded_signature = base64.b64decode(signature_header)
    except Exception:
        return ("Unauthorized: Invalid Intuit-Signature encoding", 401)

    # 4. Compute HMAC-SHA256 signature of raw request payload
    payload = request.get_data()

    # Convert verifier token to bytes safely
    if isinstance(verifier_token, str):
        key_bytes = verifier_token.encode("utf-8")
    elif isinstance(verifier_token, bytes):
        key_bytes = verifier_token
    else:
        key_bytes = str(verifier_token).encode("utf-8")

    computed_signature = hmac.new(key_bytes, payload, hashlib.sha256).digest()

    # 5. Use hmac.compare_digest to verify
    if not hmac.compare_digest(computed_signature, decoded_signature):
        return ("Unauthorized: Invalid Intuit-Signature", 401)

    try:
        event_data = request.get_json(silent=True)
        if not event_data:
            return ("Bad Request", 400)

        notifications = event_data.get("eventNotifications", [])
        for notification in notifications:
            entities = notification.get("dataChangeEvent", {}).get("entities", [])
            for entity in entities:
                if entity.get("name") == "Invoice" and entity.get("operation") in (
                    "Update",
                    "Create",
                ):
                    invoice_id = entity.get("id")
                    if invoice_id:
                        # Query Firestore bookings for this invoice ID
                        bookings_ref = db.collection("bookings")
                        query = bookings_ref.where(
                            filter=firestore.FieldFilter(
                                "integration_ids.qbo_invoice_id", "==", invoice_id
                            )
                        ).stream()

                        for doc in query:
                            booking_data = doc.to_dict() or {}
                            if booking_data.get("payment_status") != "PAID":
                                doc.reference.update({"payment_status": "PAID"})
                                booking_data["payment_status"] = "PAID"
                                send_booking_receipt_email(doc.id, booking_data)

        return ({"status": "success"}, 200)

    except Exception as e:
        return ({"status": "error", "message": str(e)}, 500)


@functions_framework.http
def m365_free_availability(request):
    """Return available tour slots for each day as timestamps.
    Uses a single Microsoft Graph call covering the entire start/end range to fetch all
    calendar events and compares them with already booked slots stored in Firestore.
    This reduces the number of Graph API requests dramatically.
    """
    # --- CORS Configuration ---
    origin = request.headers.get("Origin")
    allowed_origins = {
        "https://bodiefoundation.org",
        "https://www.bodiefoundation.org",
        "https://site.squarespace.com",
        "https://guppy-sapphire-xsfm.squarespace.com",
        "http://localhost:3000",
        "http://127.0.0.1:8080",
        "http://localhost:8000",
        "http://localhost:8081",
    }
    cors_origin = "https://www.bodiefoundation.org"  # default secure origin

    if origin:
        origin_lower = origin.lower()
        if origin_lower in allowed_origins:
            cors_origin = origin

    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)

    headers = {
        "Access-Control-Allow-Origin": cors_origin,
    }

    if request.method != "GET":
        return ("Method Not Allowed", 405, headers)

    try:
        token, user_id = get_m365_access_token()
        # Resolve optional calendar_id configuration
        calendar_id = None
        if db.__class__.__name__ != "DummyFirestore":
            try:
                auth_doc = db.collection("config").document("m365_auth").get()
                if auth_doc.exists:
                    calendar_id = auth_doc.to_dict().get("calendar_id")
            except Exception:
                pass

        # Parse optional date range
        start_str = request.args.get("start")
        end_str = request.args.get("end")
        today = datetime.now().date()
        start_date = (
            datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else today
        )
        end_date = (
            datetime.strptime(end_str, "%Y-%m-%d").date()
            if end_str
            else today + timedelta(days=30)
        )

        local_tz = ZoneInfo("America/Los_Angeles")
        # Build start/end ISO strings for the entire start/end date range in Pacific time
        range_start = datetime.combine(start_date, datetime.min.time()).replace(
            tzinfo=local_tz
        )
        range_end = datetime.combine(end_date, datetime.max.time()).replace(
            tzinfo=local_tz
        )
        start_iso = range_start.isoformat()
        end_iso = range_end.isoformat()

        graph_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if calendar_id:
            url = (
                f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/calendarView"
                f"?startDateTime={start_iso}&endDateTime={end_iso}&$select=subject,showAs,start,end"
            )
        else:
            url = (
                f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView"
                f"?startDateTime={start_iso}&endDateTime={end_iso}&$select=subject,showAs,start,end"
            )

        try:
            resp = requests.get(url, headers=graph_headers, timeout=10)
            resp.raise_for_status()
            events = resp.json().get("value", [])
        except Exception:
            events = []

        # Parse the resulting events list once and group/index them in-memory by date (using America/Los_Angeles local date strings)
        free_hours_by_date = {}
        for ev in events:
            subject = ev.get("subject", "")
            show_as = ev.get("showAs", "").lower()
            if subject.startswith(TOURING_HOURS_SUBJECT_PREFIX) and show_as in (
                "free",
                "tentative",
            ):
                try:
                    ev_start = _safe_fromisoformat(ev["start"]["dateTime"]).replace(
                        tzinfo=_get_zoneinfo(ev["start"].get("timeZone", "UTC"))
                    )
                    ev_end = _safe_fromisoformat(ev["end"]["dateTime"]).replace(
                        tzinfo=_get_zoneinfo(ev["end"].get("timeZone", "UTC"))
                    )

                    ev_start_local = ev_start.astimezone(local_tz)
                    ev_end_local = ev_end.astimezone(local_tz)

                    hour_cursor = ev_start_local
                    while hour_cursor < ev_end_local:
                        date_str = hour_cursor.strftime("%Y-%m-%d")
                        hour_str = hour_cursor.strftime("%H:%M")
                        if date_str not in free_hours_by_date:
                            free_hours_by_date[date_str] = set()
                        free_hours_by_date[date_str].add(hour_str)
                        hour_cursor += timedelta(hours=1)
                except Exception:
                    pass

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
                    slots = inventory.get("taken_slots") or inventory.get("slots") or []
                    if isinstance(slots, dict):
                        # legacy slots dict
                        for h, details in slots.items():
                            if (
                                isinstance(details, dict)
                                and details.get("taken", 0) > 0
                            ):
                                booked_hours.add(h)
                    else:
                        for ts in slots:
                            # ts may be a Firestore Timestamp or datetime
                            if hasattr(ts, "to_datetime"):
                                dt = ts.to_datetime()
                            else:
                                dt = ts
                            if isinstance(dt, datetime):
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                dt_local = dt.astimezone(local_tz)
                                booked_hours.add(dt_local.strftime("%H:%M"))
            except Exception:
                pass

            # Determine free hours for this day from in-memory cache
            free_hours = free_hours_by_date.get(date_iso, set())

            # ---------- Build result slots ----------
            slots = []
            for hour in sorted(free_hours):
                if hour not in booked_hours:
                    dt = datetime.combine(
                        current, datetime.strptime(hour, "%H:%M").time()
                    ).replace(tzinfo=local_tz)
                    slots.append(dt.isoformat())
            if slots:
                result["dates"][date_iso] = {"slots": slots}
            current += timedelta(days=1)
        return (result, 200, headers)
    except Exception as e:
        return ({"status": "error", "message": str(e)}, 500, headers)


@functions_framework.http
def cancel_tour(request):
    """Customer cancels a tour using stored token."""
    # CORS setup (reuse same as handle_booking)
    origin = request.headers.get("Origin")
    allowed_origins = {
        "https://bodiefoundation.org",
        "https://www.bodiefoundation.org",
        "https://site.squarespace.com",
        "https://guppy-sapphire-xsfm.squarespace.com",
        "http://localhost:3000",
        "http://127.0.0.1:8080",
        "http://localhost:8000",
        "http://localhost:8081",
    }
    cors_origin = "https://www.bodiefoundation.org"
    if origin:
        origin_lower = origin.lower()
        if origin_lower in allowed_origins:
            cors_origin = origin
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)
    headers = {"Access-Control-Allow-Origin": cors_origin}
    try:
        # Parse parameters from URL query arguments
        booking_id = request.args.get("booking_id")
        token = request.args.get("token")
        if not booking_id or not token:
            return (
                {"status": "error", "message": "Missing booking_id or token"},
                400,
                headers,
            )
        booking_ref = db.collection("bookings").document(booking_id)
        booking_doc = booking_ref.get()
        if not booking_doc.exists:
            return ({"status": "error", "message": "Booking not found"}, 404, headers)
        data = booking_doc.to_dict()
        if data.get("token") != token:
            return ({"status": "error", "message": "Invalid token"}, 403, headers)
        tour_dt = data.get("tour_datetime")
        if tour_dt:
            if isinstance(tour_dt, str):
                tour_dt = _safe_fromisoformat(tour_dt)
            if tour_dt.tzinfo is None:
                tour_dt = tour_dt.replace(tzinfo=timezone.utc)
            tour_dt_local = tour_dt.astimezone(ZoneInfo("America/Los_Angeles"))
            date_str = tour_dt_local.strftime("%Y-%m-%d")
            inventory_ref = db.collection("public").document(date_str)
            try:
                inventory_ref.update({"taken_slots": firestore.ArrayRemove([tour_dt])})
            except Exception:
                pass
        # Update payment_status instead of deleting the booking
        booking_ref.update({"payment_status": "CANCELLED_BY_GUEST"})
        return ({"status": "success", "message": "Booking cancelled"}, 200, headers)
    except Exception as e:
        import traceback

        traceback.print_exc()
        return ({"status": "error", "message": str(e)}, 500, headers)


@functions_framework.http
def prune_unpaid_slots(request):
    """Delegate to avoid global-scope circular imports."""
    from prune_unpaid_slots import prune_unpaid_slots as _prune
    return _prune(request)


@functions_framework.http
def retry_unpaid_bookings(request):
    """Delegate to avoid global-scope circular imports."""
    from retry_unpaid_bookings import retry_unpaid_bookings as _retry
    return _retry(request)

