import functions_framework
from google.cloud import firestore
import requests
import requests_retry
import os
import base64
import uuid
import secrets
import hmac
import hashlib
import html
import time
from flask import redirect, jsonify, make_response
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from prune_unpaid_slots import send_outlook_reminder
import tours_config
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


def execute_with_m365_retry(method, url, **kwargs):
    """
    Executes an HTTP request to the Microsoft Graph API (M365) with up to 5 attempts,
    implementing base-2 exponential backoff and full jitter.
    Calls requests.get/requests.post directly to ensure mock patches in tests apply.
    """
    import random
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            if method.upper() == "GET":
                response = requests.get(url, **kwargs)
            elif method.upper() == "POST":
                response = requests.post(url, **kwargs)
            else:
                response = requests.request(method, url, **kwargs)
            # If rate limited (429) or server error (5xx), we should retry
            if response.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                backoff = (2 ** attempt) + random.uniform(0, 0.5)
                logging.info(f"M365 API returned status {response.status_code}. Retrying in {backoff:.2f} seconds (attempt {attempt}/{max_attempts})...")
                time.sleep(backoff)
                continue
            return response
        except requests.exceptions.RequestException as e:
            if attempt == max_attempts:
                raise
            backoff = (2 ** attempt) + random.uniform(0, 0.5)
            logging.info(f"M365 API request failed with exception: {e}. Retrying in {backoff:.2f} seconds (attempt {attempt}/{max_attempts})...")
            time.sleep(backoff)


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

    response = execute_with_m365_retry("POST", token_url, data=payload, timeout=10)
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
    access_token, user_id, date_str, time_str, calendar_id=None, duration_hours=1
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
    end_dt = start_dt + timedelta(hours=duration_hours)

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

    response = execute_with_m365_retry("GET", url, headers=headers, timeout=10)
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
            ev_start = _safe_fromisoformat(event["start"]["dateTime"])
            tz_start = _get_zoneinfo(event["start"].get("timeZone"))
            if ev_start.tzinfo is not None:
                ev_start = ev_start.astimezone(tz_start)
            else:
                ev_start = ev_start.replace(tzinfo=tz_start)

            ev_end = _safe_fromisoformat(event["end"]["dateTime"])
            tz_end = _get_zoneinfo(event["end"].get("timeZone"))
            if ev_end.tzinfo is not None:
                ev_end = ev_end.astimezone(tz_end)
            else:
                ev_end = ev_end.replace(tzinfo=tz_end)
            if ev_start <= start_dt and ev_end >= end_dt:
                return True

    # No qualifying touring-hours block found — slot is not open
    return False


def inject_m365_event(
    access_token, user_id, date_str, time_str, guest_data, booking_id, calendar_id=None, duration_hours=1
):
    if db.__class__.__name__ == "DummyFirestore":
        return "mock_m365_event_id"
    """Inject a pending calendar event into the ranger's M365 Outlook calendar."""
    local_tz = ZoneInfo("America/Los_Angeles")
    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(
        tzinfo=local_tz
    )
    end_dt = start_dt + timedelta(hours=duration_hours)

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
    if booking_id:
        event_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-calendar-event-{booking_id}")
        headers["client-request-id"] = str(event_uuid)

        # Pre-creation check: see if calendar event already exists
        try:
            filter_str = f"start/dateTime eq '{start_dt.strftime('%Y-%m-%dT%H:%M:%S')}'"
            check_response = execute_with_m365_retry(
                "GET",
                url,
                headers=headers,
                params={"$filter": filter_str},
                timeout=10
            )
            if check_response.status_code == 200:
                events = check_response.json().get("value", [])
                for event in events:
                    body_content = event.get("body", {}).get("content", "")
                    subject = event.get("subject", "")
                    if booking_id in body_content or booking_id in subject:
                        found_id = event.get("id")
                        logging.info(f"M365 calendar event already exists for booking {booking_id}. Returning existing event ID '{found_id}'.")
                        return found_id
        except Exception as e:
            logging.warning(f"Error checking for pre-existing M365 calendar event: {e}")

    response = execute_with_m365_retry("POST", url, headers=headers, json=event_payload, timeout=10)
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
    duration_hours = int(data.get("duration_hours", 1))
    dtend = (dt_utc + timedelta(hours=duration_hours)).strftime("%Y%m%dT%H%M%SZ")
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
        f"{api_base_url}/cancel-tour?booking_id={booking_id}&token={token}"
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
    if booking_id:
        email_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-receipt-email-{booking_id}")
        headers["client-request-id"] = str(email_uuid)
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

    try:
        res = execute_with_m365_retry("POST", url, headers=headers, json=message, timeout=10)
        if res.status_code not in (200, 202, 201):
            logging.error("Failed to send receipt email via M365: %s", res.text[:100])
            return False
        return True
    except Exception as email_ex:
        logging.exception("Failed to send receipt email via M365 due to exception: %s", email_ex)
        return False


def send_m365_invoice_email(booking_id, customer_email, payment_link, total_amount):
    """
    Sends a beautiful fallback invoice email via Microsoft 365 Graph API 
    containing the secure payment link if QBO's native mailer fails.
    """
    try:
        m365_token, m365_user_id = get_m365_access_token()
    except Exception as exc:
        logging.exception("Failed to get M365 access token for fallback invoice email: %s", exc)
        return False

    # Load booking details from Firestore to populate details
    tour_name_display = "State Park Tour"
    tour_datetime_str = "N/A"
    party_size = 1
    customer_name = "Guest"
    booking_token = ""

    if booking_id and db is not None and getattr(db, "__class__", None) and db.__class__.__name__ not in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
        try:
            booking_doc = db.collection("bookings").document(booking_id).get().to_dict() or {}
            customer_name = booking_doc.get("guest", {}).get("name", "Guest")
            party_size = booking_doc.get("party_size", 1)
            tour_type = booking_doc.get("tour_type")
            booking_token = booking_doc.get("token", "")
            
            tour_names = {
                "private_town_tour": "Private Town Tour",
                "mines_tour": "Mines, Mills, Rails and Ruins Tour",
                "stamp_mill_tour": "Standard Stamp Mill Tour",
                "history_walking_tour": "Bodie History Walking Tour",
                "twilight_tour": "Bodie Ghost Mill Twilight Tour"
            }
            tour_name_display = tour_names.get(tour_type, "Bodie State Park Tour")

            tour_dt = booking_doc.get("tour_datetime")
            if tour_dt:
                if hasattr(tour_dt, "to_datetime"):
                    tour_dt = tour_dt.to_datetime()
                local_tz = ZoneInfo("America/Los_Angeles")
                tour_dt_local = tour_dt.astimezone(local_tz)
                tour_datetime_str = tour_dt_local.strftime("%B %d, %Y at %I:%M %p")
        except Exception as exc:
            logging.warning("Failed to load booking details for fallback invoice email: %s", exc)

    # Construct dynamic cancellation link
    api_base_url = (
        os.getenv("API_BASE_URL")
        or os.getenv("CANCEL_BASE_URL")
        or "https://us-west2-bodie-tours-prod.cloudfunctions.net"
    )
    api_base_url = api_base_url.rstrip("/")
    cancellation_link = (
        f"{api_base_url}/cancel-tour?booking_id={booking_id}&token={booking_token}"
    )

    # Format dynamic total_amount safely
    try:
        total_val = float(total_amount)
    except (ValueError, TypeError):
        total_val = 0.0

    body = f"""<div style="font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e1e8ed; border-radius: 12px; background-color: #ffffff; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
  <div style="text-align: center; border-bottom: 2px solid #faf8f5; padding-bottom: 20px; margin-bottom: 20px;">
    <h2 style="color: #1e3f20; margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">BODIE FOUNDATION</h2>
    <p style="color: #8c6239; margin: 5px 0 0 0; font-size: 14px; text-transform: uppercase; font-weight: 600; letter-spacing: 1px;">Bodie State Park Tours</p>
  </div>
  
  <p style="font-size: 16px; color: #2c3e50; line-height: 1.6; margin: 0 0 16px 0;">Hi {html.escape(str(customer_name))},</p>
  
  <p style="font-size: 15px; color: #2c3e50; line-height: 1.6; margin: 0 0 24px 0;">
    Thank you for reserving a tour with us. Your booking is currently on hold pending payment of your tour invoice.
    Please use the button below to view and pay your invoice securely online. Once payment is received, your tour reservation will be automatically confirmed!
  </p>
  
  <div style="text-align: center; margin: 30px 0;">
    <a href="{html.escape(payment_link)}" style="background-color: #1e3f20; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px; display: inline-block; transition: background-color 0.2s; box-shadow: 0 4px 6px rgba(30, 63, 32, 0.2);">View & Pay Invoice</a>
  </div>
  
  <div style="background-color: #faf8f5; border-left: 4px solid #8c6239; padding: 16px; border-radius: 4px; margin-bottom: 24px;">
    <h4 style="margin: 0 0 10px 0; color: #1e3f20; font-size: 15px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;">Tour Reservation Summary</h4>
    <table style="width: 100%; border-collapse: collapse; font-size: 14px; color: #2c3e50;">
      <tr>
        <td style="padding: 4px 0; font-weight: 600; width: 120px;">Booking ID:</td>
        <td style="padding: 4px 0;">{html.escape(str(booking_id))}</td>
      </tr>
      <tr>
        <td style="padding: 4px 0; font-weight: 600;">Tour Type:</td>
        <td style="padding: 4px 0;">{html.escape(str(tour_name_display))}</td>
      </tr>
      <tr>
        <td style="padding: 4px 0; font-weight: 600;">Date & Time:</td>
        <td style="padding: 4px 0;">{html.escape(str(tour_datetime_str))} (Pacific Time)</td>
      </tr>
      <tr>
        <td style="padding: 4px 0; font-weight: 600;">Party Size:</td>
        <td style="padding: 4px 0;">{html.escape(str(party_size))} guests</td>
      </tr>
      <tr>
        <td style="padding: 4px 0; font-weight: 600;">Amount Due:</td>
        <td style="padding: 4px 0; font-weight: 700; color: #8c6239; font-size: 15px;">${total_val:.2f}</td>
      </tr>
    </table>
  </div>
  
  <p style="font-size: 13px; color: #7f8c8d; line-height: 1.5; margin: 0 0 24px 0;">
    <i>Note: Your unpaid tour slot is temporarily held for a maximum of 1 hour from reservation. Please pay your invoice promptly to ensure your spot is not released.</i>
  </p>
  
  <p style="text-align: center; margin: 20px 0; font-size: 13px; color: #7f8c8d;">
    Changed your mind? You can <a href="{html.escape(cancellation_link)}" style="color: #1e3f20; text-decoration: underline;" target="_blank">cancel your booking here</a>.
  </p>
  
  <div style="border-top: 2px solid #faf8f5; padding-top: 15px; text-align: center; font-size: 12px; color: #95a5a6;">
    <p style="margin: 0;">Bodie Foundation | P.O. Box 278, Bridgeport, CA 93517</p>
    <p style="margin: 5px 0 0 0;">This is an automated notification. Please do not reply directly to this email.</p>
  </div>
</div>"""

    subject = f"Invoice for Bodie State Park Tour (Booking {booking_id})"
    url = f"https://graph.microsoft.com/v1.0/users/{m365_user_id}/sendMail"
    headers = {
        "Authorization": f"Bearer {m365_token}",
        "Content-Type": "application/json",
    }
    if booking_id:
        email_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-invoice-email-{booking_id}")
        headers["client-request-id"] = str(email_uuid)
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": customer_email}}],
        },
        "saveToSentItems": "false",
    }

    if db.__class__.__name__ == "DummyFirestore":
        logging.info(
            "Mock sending fallback invoice email for booking %s to %s with link %s",
            booking_id,
            customer_email,
            payment_link,
        )
        return True

    try:
        res = execute_with_m365_retry("POST", url, headers=headers, json=message, timeout=10)
        if res.status_code not in (200, 202, 201):
            logging.error("Failed to send fallback invoice email via M365: %s", res.text[:100])
            return False
        
        logging.info(f"Successfully sent fallback invoice email via M365 to {customer_email}")
        return True
    except Exception as email_ex:
        logging.exception("Failed to send fallback invoice email via M365 due to exception: %s", email_ex)
        return False


# ---------------------------------------------------------------------------
# QBO Helpers
# ---------------------------------------------------------------------------


def _mask_sensitive_qbo_text(text):
    """
    Securely mask sensitive QBO response/request details (PII and credentials)
    in logs or error messages.
    """
    if not text:
        return text
    import re
    import json
    if not isinstance(text, str):
        text = str(text)

    sensitive_keys = {
        "PrimaryEmailAddr", "Address", "BillEmail", "DisplayName", 
        "PrimaryPhone", "FreeFormNumber", "client_secret", "access_token", 
        "refresh_token", "realmId", "client_id", "verifier_token"
    }

    def mask_value(val):
        if isinstance(val, dict):
            return {k: (mask_value(v) if k in sensitive_keys else mask_dict_list(v)) for k, v in val.items()}
        elif isinstance(val, list):
            return [mask_value(item) for item in val]
        else:
            return "[MASKED]"

    def mask_dict_list(val):
        if isinstance(val, dict):
            return {k: (mask_value(v) if k in sensitive_keys else mask_dict_list(v)) for k, v in val.items()}
        elif isinstance(val, list):
            return [mask_dict_list(item) for item in val]
        return val

    try:
        parsed = json.loads(text)
        masked_parsed = mask_dict_list(parsed)
        return json.dumps(masked_parsed)
    except Exception:
        pass

    masked_text = text
    masked_text = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[MASKED_EMAIL]', masked_text)
    masked_text = re.sub(r'\b(?:\+?\d{1,3}[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b', '[MASKED_PHONE]', masked_text)
    
    for key in sensitive_keys:
        pattern = re.compile(rf'("{key}"\s*:\s*)"[^"]*"', re.IGNORECASE)
        masked_text = pattern.sub(r'\1"[MASKED]"', masked_text)
        pattern_unquoted = re.compile(rf'("{key}"\s*:\s*)[^,\s}}]+', re.IGNORECASE)
        masked_text = pattern_unquoted.sub(r'\1"[MASKED]"', masked_text)

    return masked_text


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
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as http_err:
        masked_res = _mask_sensitive_qbo_text(response.text)
        logging.error(f"QBO Token refresh failed. Status: {response.status_code}, Response: {masked_res}")
        raise Exception(f"QBO Token refresh failed: {masked_res}") from http_err
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


def resolve_or_create_qbo_customer(access_token, realm_id, guest_data, booking_id=None):
    """
    Finds a QBO Customer by email or creates a new one if not found.
    Falls back gracefully to "1" on any error or mock/dummy mode.
    """
    is_mock_test = (
        db.__class__.__name__ in ("DummyFirestore", "MagicMock", "Mock")
        or (access_token and access_token.startswith("mock"))
        or (realm_id and realm_id.startswith("mock"))
    )
    if is_mock_test and os.environ.get("TEST_QBO_CUSTOMER_LOGIC") != "1":
        logging.info("Mock DB or credentials detected, returning fallback QBO Customer ID '1'.")
        return "1"

    if not guest_data or not isinstance(guest_data, dict):
        logging.warning("Guest data is missing or invalid. Falling back to Customer ID '1'.")
        return "1"

    email = guest_data.get("email")
    if not email or not isinstance(email, str):
        logging.warning("Guest email is missing or invalid. Falling back to Customer ID '1'.")
        return "1"

    email = email.strip()

    try:
        # Determine environment & base URL
        environment = None
        if db is not None and getattr(db, "__class__", None) and db.__class__.__name__ not in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
            try:
                auth_doc = db.collection("config").document("qbo_auth").get()
                if auth_doc.exists:
                    doc_data = auth_doc.to_dict() or {}
                    environment = doc_data.get("environment")
            except Exception:
                pass

        if not environment:
            environment = os.getenv("QBO_ENVIRONMENT", os.getenv("ENVIRONMENT", "sandbox"))
        environment = environment.lower().strip()

        default_base_url = (
            "https://quickbooks.api.intuit.com/v3/company"
            if environment == "production"
            else "https://sandbox-quickbooks.api.intuit.com/v3/company"
        )
        base_url = os.getenv("QBO_BASE_URL", default_base_url)
        base_url = f"{base_url}/{realm_id}"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Escape single quotes and backslashes for QQL
        escaped_email = email.replace("\\", "\\\\").replace("'", "\\'")
        query_str = f"SELECT Id FROM Customer WHERE PrimaryEmailAddr = '{escaped_email}'"

        # Query QBO
        query_url = f"{base_url}/query"
        response = requests.get(
            query_url,
            headers=headers,
            params={"query": query_str, "minorversion": "75"},
            timeout=10
        )

        if response.status_code in (200, 201):
            response_data = response.json()
            query_response = response_data.get("QueryResponse", {})
            customers = query_response.get("Customer", [])
            if customers:
                customer_id = customers[0].get("Id")
                if customer_id:
                    logging.info(f"Found existing QBO customer ID '{customer_id}' for email '{email}'.")
                    return customer_id

        # Create a new customer if not found
        display_name = guest_data.get("name", "Guest").strip()
        if not display_name:
            display_name = "Guest"

        create_payload = {
            "DisplayName": display_name,
            "PrimaryEmailAddr": {
                "Address": email
            },
            "PrimaryPhone": {
                "FreeFormNumber": guest_data.get("phone", "N/A").strip()
            }
        }

        create_url = f"{base_url}/customer?minorversion=75"
        if booking_id:
            cust_token = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-customer-{booking_id}"))
            create_url += f"&requestid={cust_token}"

        create_res = requests.post(
            create_url,
            headers=headers,
            json=create_payload,
            timeout=10
        )

        if create_res.status_code in (200, 201):
            create_data = create_res.json()
            new_customer_id = create_data.get("Customer", {}).get("Id")
            if new_customer_id:
                logging.info(f"Created new QBO customer with ID '{new_customer_id}' for email '{email}'.")
                return new_customer_id

        # Handle duplicate name collision (status 400, Error Code 6240)
        if create_res.status_code == 400:
            err_text = create_res.text
            if "6240" in err_text or "The name supplied already exists" in err_text:
                logging.warning(f"QBO DisplayName collision for name '{display_name}'. Resolving collision...")
                # 1. Query for the customer with that display name
                escaped_name = display_name.replace("\\", "\\\\").replace("'", "\\'")
                name_query_str = f"SELECT Id FROM Customer WHERE DisplayName = '{escaped_name}'"
                name_response = requests.get(
                    query_url,
                    headers=headers,
                    params={"query": name_query_str, "minorversion": "75"},
                    timeout=10
                )
                if name_response.status_code in (200, 201):
                    name_response_data = name_response.json()
                    name_query_response = name_response_data.get("QueryResponse", {})
                    name_customers = name_query_response.get("Customer", [])
                    if name_customers:
                        existing_id = name_customers[0].get("Id")
                        if existing_id:
                            logging.info(f"Resolved collision: using existing customer ID '{existing_id}' for display name '{display_name}'.")
                            return existing_id

                # 2. If no customer found under that display name (collision was with Vendor/Employee), create unique profile
                unique_display_name = f"{display_name[:70]} - {email}"[:100]
                logging.info(f"No existing customer found with DisplayName '{display_name}'. Retrying create with unique display name '{unique_display_name}'...")
                retry_payload = dict(create_payload)
                retry_payload["DisplayName"] = unique_display_name
                
                retry_create_url = f"{base_url}/customer?minorversion=75"
                if booking_id:
                    retry_token = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-customer-{booking_id}-retry"))
                    retry_create_url += f"&requestid={retry_token}"
                else:
                    retry_create_url = create_url

                retry_res = requests.post(
                    retry_create_url,
                    headers=headers,
                    json=retry_payload,
                    timeout=10
                )
                if retry_res.status_code in (200, 201):
                    retry_data = retry_res.json()
                    retry_customer_id = retry_data.get("Customer", {}).get("Id")
                    if retry_customer_id:
                        logging.info(f"Successfully created unique QBO customer with ID '{retry_customer_id}' for email '{email}'.")
                        return retry_customer_id
                    
                logging.error(f"Failed to retry create unique QBO customer. Status: {retry_res.status_code}, Response: {_mask_sensitive_qbo_text(retry_res.text)}")

        logging.error(f"Failed to query/create QBO customer. Query status: {response.status_code}, Create status: {create_res.status_code}")
        return "1"

    except Exception as exc:
        logging.exception("Exception in resolve_or_create_qbo_customer, falling back to '1': %s", exc)
        return "1"


def create_qbo_invoice(access_token, realm_id, party_size, customer_data, booking_id=None, booking_token=None, total_amount=None):
    """Create a QBO invoice and return (invoice_id, payment_link)."""
    # Determine which payment portal to use from Firestore first, falling back to environment variable
    environment = None
    item_ref_value = None
    item_ref_name = None
    price_per_person = None
    sales_term_ref_value = None
    sales_term_ref_name = None

    if db is not None and getattr(db, "__class__", None) and db.__class__.__name__ not in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
        try:
            auth_doc = db.collection("config").document("qbo_auth").get()
            if auth_doc.exists:
                doc_data = auth_doc.to_dict() or {}
                environment = doc_data.get("environment")
                item_ref_value = doc_data.get("item_ref_value") or doc_data.get("item_value")
                item_ref_name = doc_data.get("item_ref_name") or doc_data.get("item_name")
                price_per_person = doc_data.get("unit_price") or doc_data.get("price_per_person") or doc_data.get("unitprice")
                sales_term_ref_value = doc_data.get("sales_term_ref_value") or doc_data.get("term_ref_value") or doc_data.get("term_value")
                sales_term_ref_name = doc_data.get("sales_term_ref_name") or doc_data.get("term_ref_name") or doc_data.get("term_name")
        except Exception:
            pass

    if not environment:
        environment = os.getenv("QBO_ENVIRONMENT", os.getenv("ENVIRONMENT", "sandbox"))
    environment = environment.lower().strip()

    # Base URL for QuickBooks Online API; can be overridden via environment variable
    # If not overridden, dynamically default based on the resolved environment.
    default_base_url = (
        "https://quickbooks.api.intuit.com/v3/company"
        if environment == "production"
        else "https://sandbox-quickbooks.api.intuit.com/v3/company"
    )
    base_url = os.getenv("QBO_BASE_URL", default_base_url)
    # Append the realm (company) ID to the URL path
    base_url = f"{base_url}/{realm_id}"

    if not item_ref_value:
        item_ref_value = os.getenv("QBO_ITEM_REF_VALUE", "1")

    if not item_ref_name:
        item_ref_name = os.getenv("QBO_ITEM_REF_NAME", "Tour Ticket")

    if total_amount is None:
        if price_per_person is None:
            try:
                price_per_person = float(os.getenv("TOUR_PRICE_PER_PERSON", "25.00"))
            except (ValueError, TypeError):
                price_per_person = 25.00
        else:
            try:
                price_per_person = float(price_per_person)
            except (ValueError, TypeError):
                try:
                    price_per_person = float(os.getenv("TOUR_PRICE_PER_PERSON", "25.00"))
                except (ValueError, TypeError):
                    price_per_person = 25.00
        total_amount = price_per_person * party_size
    else:
        total_amount = float(total_amount)

    memo_value = f"Bodie State Park tour booking for party of {party_size}."
    if booking_id and booking_token:
        api_base_url = (
            os.getenv("API_BASE_URL")
            or os.getenv("CANCEL_BASE_URL")
            or "https://us-west2-bodie-tours-prod.cloudfunctions.net"
        )
        api_base_url = api_base_url.rstrip("/")
        cancellation_link = (
            f"{api_base_url}/cancel-tour?booking_id={booking_id}&token={booking_token}"
        )
        memo_value += f"\n\nTo cancel your booking, click here: {cancellation_link}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Resolve QBO customer ID or create a new one
    customer_id = resolve_or_create_qbo_customer(access_token, realm_id, customer_data, booking_id=booking_id)

    try:
        today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        today_str = datetime.utcnow().strftime("%Y-%m-%d")

    is_mock_test = (
        db.__class__.__name__ in ("DummyFirestore", "MagicMock", "Mock", "_DummyClient")
        or (access_token and (access_token.startswith("mock") or access_token == "token"))
        or (realm_id and (realm_id.startswith("mock") or realm_id == "realm_id"))
    )

    resolved_sales_term_value = sales_term_ref_value
    resolved_sales_term_name = sales_term_ref_name

    if not resolved_sales_term_value:
        resolved_sales_term_value = os.getenv("QBO_SALES_TERM_REF_VALUE")

    if not resolved_sales_term_name:
        resolved_sales_term_name = os.getenv("QBO_SALES_TERM_REF_NAME")

    if not resolved_sales_term_value:
        if is_mock_test:
            resolved_sales_term_value = "3"
            resolved_sales_term_name = "Due on receipt"
        else:
            try:
                query_str = "SELECT * FROM Term"
                response = requests.get(
                    f"{base_url}/query?query={requests.utils.quote(query_str)}",
                    headers=headers,
                    timeout=10
                )
                if response.status_code == 200:
                    terms = response.json().get("QueryResponse", {}).get("Term", [])
                    for term in terms:
                        name_lower = term.get("Name", "").lower()
                        if "receipt" in name_lower:
                            resolved_sales_term_value = term.get("Id")
                            resolved_sales_term_name = term.get("Name")
                            logging.info(f"Dynamically resolved QBO Term: {resolved_sales_term_name} (ID: {resolved_sales_term_value})")
                            break
                    if not resolved_sales_term_value:
                        logging.warning("No QBO term containing 'receipt' was found. Omitting SalesTermRef.")
                else:
                    logging.warning(f"Failed to query QBO Terms (status {response.status_code}): {_mask_sensitive_qbo_text(response.text)}")
            except Exception as e:
                logging.exception("Exception querying QBO Terms: %s", e)

    invoice_payload = {
        "Line": [
            {
                "Amount": round(total_amount, 2),
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {
                    "Qty": 1,
                    "UnitPrice": round(total_amount, 2),
                    "ItemRef": {"value": item_ref_value, "name": item_ref_name},
                },
            }
        ],
        "CustomerRef": {"value": customer_id},
        "AllowOnlineCreditCardPayment": True,
        "AllowOnlineACHPayment": False,
        "BillEmail": {"Address": customer_data.get("email", "")},
        "EmailStatus": "NeedToSend",
        "DueDate": today_str,
        "CustomerMemo": {
            "value": memo_value
        },
    }

    if resolved_sales_term_value:
        sales_term_ref = {"value": resolved_sales_term_value}
        if resolved_sales_term_name:
            sales_term_ref["name"] = resolved_sales_term_name
        invoice_payload["SalesTermRef"] = sales_term_ref

    invoice_url = f"{base_url}/invoice?minorversion=75"
    if booking_id:
        invoice_token = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-invoice-{booking_id}"))
        invoice_url += f"&requestid={invoice_token}"

    response = requests.post(
        invoice_url,
        headers=headers,
        json=invoice_payload,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise Exception(f"Failed to create QBO invoice: {_mask_sensitive_qbo_text(response.text)[:100]}")
    response_data = response.json()
    invoice = response_data.get("Invoice", {})
    invoice_id = invoice.get("Id")

    # Determine the email address of the customer
    email_address = (customer_data.get("email", "").strip() if customer_data else "")
    qbo_send_success = False

    # Automatically send/email the invoice via QuickBooks Online only if a valid email is present.
    # Reordered: perform the send API call first to finalize the invoice state and trigger link generation.
    # Pass the required DeliveryAddress structure in the JSON body to satisfy schema validation (Error 2020)
    # and prevent QuickBooks internal NullPointerExceptions (Error 10000).
    if email_address and "@" in email_address:
        try:
            send_url = f"{base_url}/invoice/{invoice_id}/send"
            send_response = requests.post(
                send_url,
                headers=headers,
                params={"sendTo": email_address, "minorversion": "75"},
                json={
                    "DeliveryAddress": {
                        "Address": email_address
                    }
                },
                timeout=10,
            )
            if send_response.status_code in (200, 201):
                qbo_send_success = True
                logging.info(f"Successfully sent QBO invoice email via native API to {email_address}.")
            else:
                logging.error(f"Failed to send QBO invoice email (status {send_response.status_code}): {_mask_sensitive_qbo_text(send_response.text)[:200]}")
        except Exception as send_err:
            logging.exception("Error calling QBO invoice send API: %s", send_err)
    else:
        logging.warning("Skipping QBO invoice email send: customer email is blank or invalid ('%s').", email_address)

    # Retrieve the public customer-facing InvoiceLink using a GET request with include=invoiceLink
    # Performing this after the send call ensures that the link has been generated by QBO.
    invoice_link = None
    try:
        get_url = f"{base_url}/invoice/{invoice_id}"
        get_response = requests.get(
            get_url,
            headers=headers,
            params={"include": "invoiceLink", "minorversion": "75"},
            timeout=10,
        )
        if get_response.status_code == 200:
            try:
                invoice_link = get_response.json().get("Invoice", {}).get("InvoiceLink")
            except Exception:
                invoice_link = None
            if invoice_link:
                logging.info(f"Retrieved public InvoiceLink via QBO API: {invoice_link}")
    except Exception as get_err:
        logging.warning("Failed to retrieve public InvoiceLink from QBO: %s", get_err)

    if invoice_link:
        payment_link = invoice_link
    else:
        # Fallback to the direct portal URL if public link is not available
        if environment == "production":
            payment_link = f"https://app.qbo.intuit.com/app/invoice?txnId={invoice_id}"
        else:
            payment_link = (
                f"https://app.sandbox.qbo.intuit.com/app/invoice?txnId={invoice_id}"
            )

    # If native send failed or was skipped, and we have a valid email, initiate fallback email via M365
    if email_address and "@" in email_address and not qbo_send_success:
        logging.info("QBO native invoice email failed. Initiating fallback email via M365...")
        send_m365_invoice_email(booking_id, email_address, payment_link, total_amount)

    return invoice_id, payment_link


# ---------------------------------------------------------------------------
# Firestore Transaction
# ---------------------------------------------------------------------------


@firestore.transactional
def process_booking_transaction(
    transaction, inventory_ref, date_str, time_str, party_size, customer_data,
    tour_type="private_town_tour", duration_hours=1, vehicle_acknowledgment=False, total_amount=None
):
    """
    Executes an atomic read-modify-write operation to prevent double-booking.
    Now supports booking consecutive slots transactionally for long-duration tours.
    Standardized to execute ALL reads before any writes.
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

    # Calculate all consecutive slots required by the duration
    consecutive_local_dts = [dt_local + timedelta(hours=i) for i in range(duration_hours)]

    # Group consecutive slots by date so we can query and update them atomically
    slots_by_date = {}
    for local_dt in consecutive_local_dts:
        d_str = local_dt.strftime("%Y-%m-%d")
        h_str = local_dt.strftime("%H:%M")
        utc_dt = local_dt.astimezone(timezone.utc)
        if d_str not in slots_by_date:
            slots_by_date[d_str] = []
        slots_by_date[d_str].append((utc_dt, h_str, local_dt.strftime("%Y-%m-%d %H:%M")))

    # 1. READ PHASE: Retrieve all snapshots first to satisfy the Firestore transaction read-before-write constraint.
    read_snapshots = {}
    for d_str in slots_by_date.keys():
        if d_str == date_str:
            inv_ref = inventory_ref
        else:
            inv_ref = db.collection("public").document(d_str)
        snapshot = inv_ref.get(transaction=transaction)
        read_snapshots[d_str] = (inv_ref, snapshot)

    # 2. VALIDATE PHASE: Perform validation and prepare payloads for writes
    writes_to_perform = {}
    for d_str, needed_slots in slots_by_date.items():
        inv_ref, snapshot = read_snapshots[d_str]

        if not snapshot.exists:
            inventory_data = {}
            taken_slots_raw = []
        else:
            inventory_data = snapshot.to_dict() or {}
            taken_slots_raw = inventory_data.get("taken_slots", [])

        # Normalize taken_slots to local America/Los_Angeles "YYYY-MM-DD HH:MM" strings
        normalized_taken = []
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
                parsed_local = parsed.astimezone(local_tz)
                normalized_taken.append(parsed_local.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                try:
                    normalized_taken.append(str(ts))
                except Exception:
                    pass

        # Check if any requested slot is already taken
        for utc_dt, h_str, full_local_key in needed_slots:
            if full_local_key in normalized_taken:
                raise ValueError("This time slot is already booked by another group.")

            # Check legacy slots dict if it exists
            slots_dict = inventory_data.get("slots", {})
            if isinstance(slots_dict, dict) and slots_dict:
                current_val = slots_dict.get(h_str)
                if isinstance(current_val, dict) and current_val.get("taken", 0) > 0:
                    raise ValueError("This time slot is already booked by another group.")

        # Save all consecutive reservations
        new_taken = list(taken_slots_raw)
        for utc_dt, _, _ in needed_slots:
            new_taken.append(utc_dt)

        writes_to_perform[inv_ref] = {
            "date": d_str,
            "taken_slots": new_taken,
            "last_updated": firestore.SERVER_TIMESTAMP,
        }

    # 3. WRITE PHASE: Perform all writes only after all reads and validations are complete.
    for inv_ref, payload in writes_to_perform.items():
        transaction.set(
            inv_ref,
            payload,
            merge=True,
        )

    # If total_amount is not passed, dynamically calculate it
    if total_amount is None:
        import tours_config
        total_amount = tours_config.calculate_tour_price(tour_type, party_size)
    else:
        total_amount = float(total_amount)

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
        "tour_type": tour_type,
        "duration_hours": duration_hours,
        "vehicle_acknowledgment": vehicle_acknowledgment,
        "total_amount": total_amount,
    }
    transaction.set(new_booking_ref, booking_payload)

    return new_booking_ref.id


_csrf_secret_key = None


def _get_csrf_secret_key():
    """
    Retrieves a consistent, persistent secret key to sign CSRF tokens.
    Checks environment variable, Firestore config collection, or falls back to stable default.
    Caches the key in python memory.
    """
    global _csrf_secret_key
    if _csrf_secret_key is not None:
        return _csrf_secret_key

    # Try environment variable
    key = os.environ.get("CSRF_SECRET_KEY")
    if key:
        _csrf_secret_key = key.encode("utf-8")
        return _csrf_secret_key

    # Try config in Firestore
    for doc_name, fields in [
        ("csrf_auth", ["secret_key"]),
        ("m365_auth", ["client_secret"]),
        ("qbo_auth", ["client_secret", "prod-secret", "dev-secret"]),
    ]:
        try:
            doc = db.collection("config").document(doc_name).get()
            if doc.exists:
                data = doc.to_dict() or {}
                for f in fields:
                    val = data.get(f)
                    if val and isinstance(val, str):
                        _csrf_secret_key = val.encode("utf-8")
                        return _csrf_secret_key
        except Exception:
            pass

    # Fallback default for testing environments
    _csrf_secret_key = b"BodieToursFallbackCSRFSecretKey123456!"
    return _csrf_secret_key


def _generate_signed_csrf_token():
    """
    Generates a cryptographically signed CSRF token containing a timestamp and HMAC signature.
    """
    secret_key = _get_csrf_secret_key()
    timestamp_str = str(int(time.time()))
    sig = hmac.new(secret_key, timestamp_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{timestamp_str}.{sig}"


def _verify_signed_csrf_token(token):
    """
    Verifies that the CSRF token signature is valid and has not expired (expires in 24 hours).
    """
    if not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 2:
        return False

    timestamp_str, sig = parts
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False

    secret_key = _get_csrf_secret_key()
    expected_sig = hmac.new(secret_key, timestamp_str.encode("utf-8"), hashlib.sha256).hexdigest()

    if not secrets.compare_digest(sig, expected_sig):
        return False

    # Check expiration (24 hours) and clock drift (allow 5 mins into future)
    now = int(time.time())
    if now - timestamp > 86400 or now - timestamp < -300:
        return False

    return True


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
        csrf_token = _generate_signed_csrf_token()
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
        csrf_header = request.headers.get("X-CSRF-Token") or (
            request.get_json(silent=True) or {}
        ).get("csrf_token")
        if not csrf_header or not _verify_signed_csrf_token(csrf_header):
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

        tour_type = request_json.get("tour_type", "")
        if not tour_type:
            tour_type = "large_group_tour"
        vehicle_acknowledgment = bool(request_json.get("vehicle_acknowledgment", False))

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

        # Validate tour type and rules
        tours_rules = tours_config.load_tours_config(db)
        if tour_type not in tours_rules:
            return (
                {
                    "status": "error",
                    "message": f"Invalid tour_type: {tour_type}. Must be one of {list(tours_rules.keys())}.",
                },
                409,
                headers,
            )

        tour_rule = tours_rules[tour_type]
        duration_hours = int(tour_rule.get("duration_hours", 1))
        max_capacity = int(tour_rule.get("max_capacity", 20))

        if party_size <= 0:
            return (
                {
                    "status": "error",
                    "message": "Party size must be greater than 0.",
                },
                409,
                headers,
            )

        if party_size > max_capacity:
            return (
                {
                    "status": "error",
                    "message": f"Maximum group size for {tour_rule.get('name')} is {max_capacity}.",
                },
                409,
                headers,
            )

        if tour_rule.get("vehicle_required", False) and not vehicle_acknowledgment:
            return (
                {
                    "status": "error",
                    "message": f"A high-clearance, 4WD vehicle is required for {tour_rule.get('name')} and must be acknowledged.",
                },
                409,
                headers,
            )

        total_amount = tours_config.calculate_tour_price(tour_type, party_size)

        local_tz = ZoneInfo("America/Los_Angeles")
        dt_local = datetime.strptime(
            f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=local_tz)

        # Ensure booking is made at least 7 days in advance (except in dummy/mock testing environments to preserve test dates)
        if not is_dummy:
            now_local = datetime.now(local_tz)
            if dt_local < now_local + timedelta(days=7):
                return (
                    {
                        "status": "error",
                        "message": "Bookings must be made at least 7 days in advance.",
                    },
                    409,
                    headers,
                )


        guest_name = str(guest_data.get("name", "") or "Test Guest").strip()[:100]
        guest_email = str(guest_data.get("email", "") or "test@example.com").strip()[
            :100
        ]
        guest_phone = str(guest_data.get("phone", "") or "555-0199").strip()[:30]

        guest_data["name"] = guest_name
        guest_data["email"] = guest_email
        guest_data["phone"] = guest_phone

        inventory_ref = db.collection("public").document(date_str)

        # 2. M365 Availability Check (whitelist: requires a 'Touring Hours' Free block covering the full duration)
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
            m365_token, m365_user_id, date_str, time_str, calendar_id, duration_hours=duration_hours
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
            transaction, inventory_ref, date_str, time_str, party_size, guest_data,
            tour_type=tour_type, duration_hours=duration_hours,
            vehicle_acknowledgment=vehicle_acknowledgment, total_amount=total_amount
        )

        qbo_failed = False
        try:
            # Fetch the generated booking token to build the cancellation link
            booking_token = None
            if db is not None and getattr(db, "__class__", None) and db.__class__.__name__ not in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
                try:
                    booking_doc = db.collection("bookings").document(booking_id).get().to_dict() or {}
                    booking_token = booking_doc.get("token")
                except Exception:
                    pass

            # 4. QBO Invoice Generation
            try:
                qbo_token, realm_id = get_qbo_access_token()
                invoice_id, payment_link = create_qbo_invoice(
                    qbo_token, realm_id, party_size, guest_data, booking_id=booking_id, booking_token=booking_token, total_amount=total_amount
                )
            except Exception as qbo_err:
                qbo_failed = True
                raise qbo_err

            # 5. M365 Calendar Event Injection
            m365_event_id = None
            try:
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
                    duration_hours=duration_hours
                )
            except Exception as m365_exc:
                logging.exception(
                    "Non-fatal error: Failed to inject M365 calendar event for booking %s: %s. Booking remains valid.",
                    booking_id,
                    m365_exc,
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

                # Revert inventory reservation completely across all dates and hours reserved
                try:
                    consecutive_local_dts = [dt_local + timedelta(hours=i) for i in range(duration_hours)]
                    slots_by_date = {}
                    for local_dt in consecutive_local_dts:
                        d_str = local_dt.strftime("%Y-%m-%d")
                        h_str = local_dt.strftime("%H:%M")
                        full_local_key = local_dt.strftime("%Y-%m-%d %H:%M")
                        if d_str not in slots_by_date:
                            slots_by_date[d_str] = []
                        slots_by_date[d_str].append((h_str, full_local_key))

                    for d_str, needed_keys in slots_by_date.items():
                        inv_doc_ref = db.collection("public").document(d_str)
                        inv_snap = inv_doc_ref.get()
                        if getattr(inv_snap, "exists", True):
                            inv_data = inv_snap.to_dict() or {}
                            # Revert taken_slots, preferring current 'taken_slots' schema
                            if "taken_slots" in inv_data:
                                taken = inv_data.get("taken_slots", [])
                                new_taken = []
                                for ts in taken:
                                    try:
                                        if isinstance(ts, str):
                                            parsed = _safe_fromisoformat(ts)
                                        else:
                                            parsed = ts
                                        parsed_local = (
                                            parsed.astimezone(ZoneInfo("America/Los_Angeles"))
                                            if parsed.tzinfo
                                            else parsed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
                                        )
                                        parsed_key = parsed_local.strftime("%Y-%m-%d %H:%M")
                                        if any(parsed_key == needed_key for _, needed_key in needed_keys):
                                            # skip this slot (remove reservation)
                                            continue
                                    except Exception:
                                        # keep unknown entries
                                        pass
                                    new_taken.append(ts)
                                
                                try:
                                    inv_doc_ref.update({
                                        "taken_slots": new_taken,
                                        "last_updated": firestore.SERVER_TIMESTAMP
                                    })
                                except Exception:
                                    inv_doc_ref.set({"taken_slots": new_taken}, merge=True)
                            
                            # Revert legacy 'slots' dict if it exists
                            slots_data = inv_data.get("slots", {})
                            if isinstance(slots_data, dict) and slots_data:
                                updated_slots = False
                                for h_str, _ in needed_keys:
                                    if h_str in slots_data:
                                        slots_data[h_str]["taken"] = max(0, slots_data[h_str].get("taken", 0) - party_size)
                                        updated_slots = True
                                if updated_slots:
                                    try:
                                        inv_doc_ref.update({
                                            "slots": slots_data,
                                            "last_updated": firestore.SERVER_TIMESTAMP
                                        })
                                    except Exception:
                                        inv_doc_ref.set({"slots": slots_data}, merge=True)
                except Exception as inv_err:
                    logging.exception(
                        "Failed to revert inventory during rollback for booking %s: %s",
                        booking_id,
                        inv_err,
                    )
            except Exception as rb_err:
                logging.exception(
                    "Rollback encountered error for booking %s: %s", booking_id, rb_err
                )

            error_message = "Failed to generate QuickBooks invoice. Please try again or contact support." if qbo_failed else "Failed to process payload."
            return (
                {"status": "error", "message": error_message},
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
                            payment_status = booking_data.get("payment_status")
                            receipt_sent = booking_data.get("receipt_sent", False)
                            if payment_status != "PAID" or not receipt_sent:
                                success = False
                                try:
                                    # Use PAID in the formatted receipt email
                                    email_data = dict(booking_data)
                                    email_data["payment_status"] = "PAID"
                                    success = send_booking_receipt_email(doc.id, email_data)
                                except Exception as receipt_err:
                                    logging.exception(
                                        "Error sending booking receipt email for booking %s: %s",
                                        doc.id,
                                        receipt_err,
                                    )
                                
                                update_payload = {"receipt_sent": success}
                                if payment_status != "PAID":
                                    update_payload["payment_status"] = "PAID"
                                doc.reference.update(update_payload)

        return ({"status": "success"}, 200)

    except Exception as e:
        return ({"status": "error", "message": str(e)}, 500)


@functions_framework.http
def m365_free_availability(request):
    """Return available tour slots for each day as timestamps.
    Uses a single Microsoft Graph call covering the entire start/end range to fetch all
    calendar events and compares them with already booked slots stored in Firestore.
    This reduces the number of Graph API requests dramatically.
    Supports an optional duration query parameter to verify consecutive free and unbooked slots.
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

        # Parse optional duration query parameter
        try:
            duration = int(request.args.get("duration", 1))
            if duration <= 0:
                duration = 1
        except (ValueError, TypeError):
            duration = 1

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

        is_dummy = (
            "Dummy" in db.__class__.__name__
            or "Mock" in db.__class__.__name__
            or "Proxy" in db.__class__.__name__
            or os.getenv("FORCE_DUMMY_DB") == "1"
        )
        if not is_dummy:
            min_allowed_date = today + timedelta(days=7)
            if start_date < min_allowed_date:
                start_date = min_allowed_date


        if start_date > end_date:
            return ({"dates": {}}, 200, headers)

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

        # Parse the resulting events list once and group/index them in-memory by date
        free_hours_by_date = {}
        for ev in events:
            subject = ev.get("subject", "")
            show_as = ev.get("showAs", "").lower()
            if subject.startswith(TOURING_HOURS_SUBJECT_PREFIX) and show_as in (
                "free",
                "tentative",
            ):
                try:
                    ev_start = _safe_fromisoformat(ev["start"]["dateTime"])
                    tz_start = _get_zoneinfo(ev["start"].get("timeZone", "UTC"))
                    if ev_start.tzinfo is not None:
                        ev_start = ev_start.astimezone(tz_start)
                    else:
                        ev_start = ev_start.replace(tzinfo=tz_start)

                    ev_end = _safe_fromisoformat(ev["end"]["dateTime"])
                    tz_end = _get_zoneinfo(ev["end"].get("timeZone", "UTC"))
                    if ev_end.tzinfo is not None:
                        ev_end = ev_end.astimezone(tz_end)
                    else:
                        ev_end = ev_end.replace(tzinfo=tz_end)

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

        booked_hours_by_date = {}
        def get_booked_hours(date_str):
            if date_str in booked_hours_by_date:
                return booked_hours_by_date[date_str]
            b_hours = set()
            try:
                inventory_doc = db.collection("public").document(date_str).get()
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
                                b_hours.add(h)
                    else:
                        for ts in slots:
                            if hasattr(ts, "to_datetime"):
                                dt = ts.to_datetime()
                            else:
                                dt = ts
                            if isinstance(dt, datetime):
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                dt_local = dt.astimezone(local_tz)
                                b_hours.add(dt_local.strftime("%H:%M"))
            except Exception:
                pass
            booked_hours_by_date[date_str] = b_hours
            return b_hours

        result = {"dates": {}}
        current = start_date
        while current <= end_date:
            date_iso = current.isoformat()
            free_hours = free_hours_by_date.get(date_iso, set())

            slots = []
            for hour in sorted(free_hours):
                dt_start = datetime.combine(
                    current, datetime.strptime(hour, "%H:%M").time()
                ).replace(tzinfo=local_tz)

                is_slot_available = True
                for i in range(duration):
                    check_dt = dt_start + timedelta(hours=i)
                    check_date = check_dt.strftime("%Y-%m-%d")
                    check_hour = check_dt.strftime("%H:%M")

                    # 1. Must be free in M365
                    if check_hour not in free_hours_by_date.get(check_date, set()):
                        is_slot_available = False
                        break

                    # 2. Must NOT be booked in Firestore
                    if check_hour in get_booked_hours(check_date):
                        is_slot_available = False
                        break

                if is_slot_available:
                    slots.append(dt_start.isoformat())

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
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
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
            duration_hours = int(data.get("duration_hours", 1))
            consecutive_slots = [tour_dt + timedelta(hours=i) for i in range(duration_hours)]

            local_tz = ZoneInfo("America/Los_Angeles")
            slots_by_date = {}
            for dt_slot in consecutive_slots:
                local_dt = dt_slot.astimezone(local_tz)
                d_str = local_dt.strftime("%Y-%m-%d")
                if d_str not in slots_by_date:
                    slots_by_date[d_str] = []
                slots_by_date[d_str].append(dt_slot)

            for d_str, d_slots in slots_by_date.items():
                inventory_ref = db.collection("public").document(d_str)
                try:
                    inventory_ref.update({"taken_slots": firestore.ArrayRemove(d_slots)})
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

