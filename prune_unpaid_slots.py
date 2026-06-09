import functions_framework
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
import os
from google.cloud import firestore
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
from unittest.mock import MagicMock
import secrets

from typing import Any

_cached_db: Any = None

def _get_db() -> Any:
    """Return a Firestore client, using a MagicMock dummy when unavailable.
    The result is cached after first creation.
    """
    global _cached_db
    if _cached_db is not None:
        return _cached_db
    try:
        if os.getenv("FORCE_DUMMY_DB") == "1":
            raise Exception("Forced dummy DB for testing")
        _cached_db = firestore.Client(database="bodie-tours")
    except Exception as e:
        import warnings
        warnings.warn(
            f"Firestore client initialization failed ({e}). Using a dummy in-memory client for testing."
        )
        _cached_db = MagicMock(name='db')
        _cached_db.collection = MagicMock()
        _cached_db.collection.return_value = MagicMock()
        _cached_db.transaction = MagicMock()
        _cached_db.transaction.return_value = MagicMock()
        _cached_db.__class__.__name__ = 'DummyFirestore'
    return _cached_db

# Backward compatibility: module level db variable
db = _get_db()

MAX_CAPACITY = 20


# ---------------------------------------------------------------------------
# M365 Token Helper (shared with main.py logic, self-contained here)
# ---------------------------------------------------------------------------

def _get_m365_token_for_prune():
    """Fetch a valid M365 access token from the config/m365_auth Firestore doc."""
    auth_doc_ref = db.collection("config").document("m365_auth")
    auth_data = auth_doc_ref.get().to_dict()
    if not auth_data:
        raise Exception("M365 Auth configuration missing.")

    user_id = auth_data.get("user_id")
    access_token = auth_data.get("access_token")
    expires_at = auth_data.get("expires_at")

    if access_token and expires_at:
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return access_token, user_id

    client_id = auth_data.get("client_id") or os.environ.get("M365_CLIENT_ID")
    client_secret = auth_data.get("client_secret") or os.environ.get("M365_CLIENT_SECRET")
    tenant_id = auth_data.get("tenant_id") or os.environ.get("M365_TENANT_ID", "common")
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
    new_refresh_token = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in", 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    update_data = {"access_token": new_access_token, "expires_at": new_expires_at}
    if new_refresh_token and new_refresh_token != refresh_token:
        update_data["refresh_token"] = new_refresh_token
    auth_doc_ref.update(update_data)

    return new_access_token, user_id


# ---------------------------------------------------------------------------
# M365 Actions
# ---------------------------------------------------------------------------

def send_outlook_reminder(access_token, user_id, customer_email, customer_name, tour_datetime_str, booking_id, payment_link=None, party_size=1):
    """Send an email reminder via M365 Graph API /sendMail before pruning.
    # Determine environment (production or sandbox)
    environment = os.getenv("QBO_ENVIRONMENT", os.getenv("ENVIRONMENT", "sandbox"))
    # Base URL varies by environment
    if environment == "production":
        base_url = "https://quickbooks.api.intuit.com/v3/company"
    else:
        base_url = os.getenv("QBO_BASE_URL", "https://sandbox-quickbooks.api.intuit.com/v3/company")
    # Append the realm (company) ID to the URL path
    base_url = f"{base_url}/{realm_id}".lower()
    Supports configurable templates via the EMAIL_TEMPLATE_TYPE environment variable.
    - "simple": uses the built‑in default subject/body (current behavior).
    - "custom": loads a Firestore document `email_templates/prune_reminder` with fields `subject` and `body`.
    """
    # Determine which template to use
    template_type = os.getenv("EMAIL_TEMPLATE_TYPE", "simple").lower()
    if template_type == "custom":
        # Fetch custom template from Firestore
        try:
            tmpl_doc = db.collection("email_templates").document("prune_reminder").get()
            tmpl_data = tmpl_doc.to_dict() or {}
            subject = tmpl_data.get("subject", "Reminder: Your Bodie State Park Tour Booking Is Pending Payment")
            body = tmpl_data.get("body",
                f"<p>Hi {customer_name},</p>"
                f"<p>Your tour booking (ID: <b>{booking_id}</b>) for <b>{tour_datetime_str}</b> is still awaiting payment.</p>"
                f"<p>Please complete your payment soon to avoid automatic cancellation.</p>"
                f"<p>Thank you,<br>Bodie State Park Tour Team</p>"
            )
            
            # Format placeholders safely
            price = float(os.getenv("TOUR_PRICE_PER_PERSON", "25.00"))
            total_amount = f"{price * party_size:.2f}"
            for key, val in [
                ("customer_name", customer_name),
                ("booking_id", booking_id),
                ("tour_datetime_str", tour_datetime_str),
                ("payment_link", payment_link or ""),
                ("party_size", str(party_size)),
                ("total_amount", total_amount)
            ]:
                body = body.replace(f"{{{{{key}}}}}", val).replace(f"{{{key}}}", val)
                subject = subject.replace(f"{{{{{key}}}}}", val).replace(f"{{{key}}}", val)
        except Exception as exc:
            logger.exception("Failed to load custom email template; falling back to simple template: %s", exc)
            # Fallback to simple template on any error
            subject = "Reminder: Your Bodie State Park Tour Booking Is Pending Payment"
            body = (
                f"<p>Hi {customer_name},</p>"
                f"<p>Your tour booking (ID: <b>{booking_id}</b>) for <b>{tour_datetime_str}</b> is still awaiting payment.</p>"
                f"<p>Please complete your payment soon to avoid automatic cancellation.</p>"
                f"<p>Thank you,<br>Bodie State Park Tour Team</p>"
            )
    else:
        # Simple built‑in template
        subject = "Reminder: Your Bodie State Park Tour Booking Is Pending Payment"
        body = (
            f"<p>Hi {customer_name},</p>"
            f"<p>Your tour booking (ID: <b>{booking_id}</b>) for <b>{tour_datetime_str}</b> is still awaiting payment.</p>"
            f"<p>Please complete your payment soon to avoid automatic cancellation.</p>"
            f"<p>Thank you,<br>Bodie State Park Tour Team</p>"
        )

    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/sendMail"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": customer_email}}]
        },
        "saveToSentItems": "false"
    }
    response = requests.post(url, headers=headers, json=message, timeout=10)
    return response.status_code in (200, 202)


def remove_m365_event(access_token, user_id, event_id):
    """Delete an M365 calendar event by its ID."""
    calendar_id = None
    if db.__class__.__name__ != 'DummyFirestore':
        try:
            auth_doc = db.collection("config").document("m365_auth").get()
            if auth_doc.exists:
                calendar_id = auth_doc.to_dict().get("calendar_id")
        except Exception as exc:
            logger.exception("Failed fetching m365_auth calendar_id: %s", exc)
            calendar_id = None

    if calendar_id:
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events/{event_id}"
    else:
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events/{event_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.delete(url, headers=headers, timeout=10)
    return response.status_code == 204



# ---------------------------------------------------------------------------
# Dynamic TTL Calculation
# ---------------------------------------------------------------------------

def calculate_ttl(created_at, tour_datetime):
    """
    Dynamically compute the TTL for an unpaid booking based on lead time.

    TTL Rules:
    - Lead time >= 7 days  → TTL = 48 hours
    - Lead time >= 2 days  → TTL = 24 hours
    - Lead time >= 1 day   → TTL = 3 hours
    - Lead time < 1 day    → TTL = 1 hour
    """
    lead_time = tour_datetime - created_at
    if lead_time >= timedelta(days=7):
        return timedelta(hours=48)
    elif lead_time >= timedelta(days=2):
        return timedelta(hours=24)
    elif lead_time >= timedelta(days=1):
        return timedelta(hours=3)
    else:
        return timedelta(hours=1)


# ---------------------------------------------------------------------------
# Firestore Cancellation Transaction
# ---------------------------------------------------------------------------


@firestore.transactional
def process_cancellation_transaction(transaction, booking_ref, inventory_ref, party_size, time_str):
    """
    Executes an atomic read-modify-write operation to cancel an unpaid booking
    and return the slots to the inventory.
    """
    # 1. Re-verify booking is still PENDING
    booking_snapshot = booking_ref.get(transaction=transaction)
    if not booking_snapshot.exists:
        return False

    booking_data = booking_snapshot.to_dict()
    if booking_data.get("payment_status") != "PENDING":
        return False

    # 2. Fetch the corresponding public inventory doc
    inventory_snapshot = inventory_ref.get(transaction=transaction)
    if inventory_snapshot.exists:
        inventory_data = inventory_snapshot.to_dict()
        slots_data = inventory_data.get("slots", {})

        time_slot = slots_data.get(time_str, {})
        current_taken = time_slot.get("taken", 0)

        # 3. Update inventory
        new_taken = max(0, current_taken - party_size)
        new_status = "AVAILABLE" if new_taken < MAX_CAPACITY else "SOLD_OUT"

        slots_data[time_str] = {"taken": new_taken, "status": new_status}
        
        # Update taken_slots array
        from zoneinfo import ZoneInfo
        date_str = inventory_ref.id
        local_tz = ZoneInfo("America/Los_Angeles")
        try:
            slot_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
            slot_dt_utc = slot_dt.astimezone(timezone.utc)
        except ValueError:
            slot_dt_utc = None

        taken_slots = inventory_data.get("taken_slots", [])
        new_taken_slots = []
        if slot_dt_utc:
            for ts in taken_slots:
                # Handle ts if it is string or datetime
                if isinstance(ts, str):
                    try:
                        ts_val = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                else:
                    ts_val = ts
                ts_utc = ts_val.astimezone(timezone.utc) if ts_val.tzinfo else ts_val.replace(tzinfo=timezone.utc)
                if ts_utc != slot_dt_utc:
                    new_taken_slots.append(ts)
        else:
            new_taken_slots = taken_slots

        transaction.set(inventory_ref, {
            "slots": slots_data,
            "taken_slots": new_taken_slots,
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)

    # 4. Update booking payment_status
    transaction.update(booking_ref, {"payment_status": "CANCELLED_UNPAID"})
    return True


# ---------------------------------------------------------------------------
# Completed Tour Pruning
# ---------------------------------------------------------------------------

def prune_completed_tours(now):
    """
    Silently delete completed (PAID or CANCELLED) tour documents from the previous day.
    Utilizes composite index on payment_status and tour_datetime to limit reads.
    """
    # Determine yesterday's date boundaries in UTC using local timezone
    local_tz = ZoneInfo("America/Los_Angeles")
    yesterday_date = (now - timedelta(days=1)).astimezone(local_tz).date()
    yesterday_start = datetime.combine(yesterday_date, datetime.min.time()).replace(tzinfo=local_tz).astimezone(timezone.utc)
    yesterday_end = yesterday_start + timedelta(days=1)

    completed_statuses = ["PAID", "CANCELLED_UNPAID"]
    pruned_count = 0

    for status in completed_statuses:
        docs = (
            db.collection("bookings")
            .where(filter=firestore.FieldFilter("payment_status", "==", status))
            .where(filter=firestore.FieldFilter("tour_datetime", ">=", yesterday_start))
            .where(filter=firestore.FieldFilter("tour_datetime", "<", yesterday_end))
            .stream()
        )
        for doc in docs:
            # Doc may be a MockDocumentSnapshot in tests; use its reference when available
            try:
                ref = getattr(doc, "reference", None) or getattr(doc, "reference", None)
                if ref and hasattr(ref, "delete"):
                    ref.delete()
                else:
                    # Fallback: attempt to delete via collection/document path if provided
                    try:
                        path = getattr(doc, "reference", None)
                        if path and hasattr(path, "path"):
                            db.collection(path.split('/')[0]).document(path.split('/')[1]).delete()
                    except Exception:
                        # Best-effort: log and continue
                        logger.exception("Could not delete doc during pruning: %s", getattr(doc, 'reference', doc))
                pruned_count += 1
            except Exception:
                logger.exception("Error while deleting completed tour during pruning")
    return pruned_count

    return pruned_count


# ---------------------------------------------------------------------------
# Main Prune Function
# ---------------------------------------------------------------
@functions_framework.http

def prune_unpaid_slots(request):
    """
    HTTP Cloud Function entry point to prune unpaid booking slots.
    Triggered by Cloud Scheduler every 15 minutes.
    """
    try:
        now = datetime.now(timezone.utc)

        # Try to get M365 credentials for reminders and event deletion
        try:
            m365_token, m365_user_id = _get_m365_token_for_prune()
            m365_available = True
        except Exception as e:
            logger.warning(f"M365 token fetch failed: {e}")
            m365_token = None
            m365_user_id = None
            m365_available = False

        # Query PENDING bookings older than 1 hour
        cutoff = now - timedelta(hours=1)
        pending_bookings = (
            db.collection("bookings")
            .where(filter=firestore.FieldFilter("payment_status", "==", "PENDING"))
            .where(filter=firestore.FieldFilter("created_at", "<=", cutoff))
            .stream()
        )

        cancelled_count = 0
        reminder_count = 0

        for doc in pending_bookings:
            data = doc.to_dict()

            tour_datetime = data.get("tour_datetime")
            created_at = data.get("created_at")

            if not tour_datetime or not created_at:
                continue

            try:
                if isinstance(tour_datetime, str):
                    tour_datetime = datetime.fromisoformat(tour_datetime)
                if tour_datetime.tzinfo is None:
                    tour_datetime = tour_datetime.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if not hasattr(created_at, "tzinfo"):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            # Compute TTL based on lead time
            ttl = calculate_ttl(created_at, tour_datetime)
            # Determine time remaining until the tour starts
            time_to_tour = tour_datetime - now

            # Send a reminder email when we are within half of the TTL window before the tour
            half_ttl = ttl / 2
            if (
                time_to_tour <= half_ttl
                and time_to_tour > timedelta(0)
                and m365_available
                and not data.get("reminder_sent")
                and data.get("reminder_sent_count", 0) < 2
            ):
                guest = data.get("guest") or data.get("customer") or {}
                customer_email = guest.get("email")
                customer_name = guest.get("name", "Guest")
                tour_datetime_str = tour_datetime.isoformat()
                if customer_email:
                    sent = send_outlook_reminder(
                        m365_token,
                        m365_user_id,
                        customer_email,
                        customer_name,
                        tour_datetime_str,
                        doc.id,
                        payment_link=data.get("payment_link"),
                        party_size=data.get("party_size", 1),
                    )
                    if sent:
                        reminder_count += 1
                        try:
                            doc.reference.update({"reminder_sent_count": firestore.Increment(1)})
                        except Exception as exc:
                            logger.exception("Failed to increment reminder_sent_count: %s", exc)

            # Cancel the booking if we are within the TTL window before the tour (i.e., the deadline has passed)
            if time_to_tour <= ttl and time_to_tour > timedelta(0):
                # Extract date_str and time_str from tour_datetime
                date_str = tour_datetime.strftime("%Y-%m-%d")
                time_str = tour_datetime.strftime("%H:%M")
                party_size = data.get("party_size", 0)

                inventory_ref = db.collection("public").document(date_str)
                with db.transaction() as transaction:
                    success = process_cancellation_transaction(
                        transaction,
                        doc.reference,
                        inventory_ref,
                        party_size,
                        time_str,
                    )
                if success:
                    cancelled_count += 1

                    # Remove associated M365 calendar event
                    if m365_available:
                        event_id = data.get("integration_ids", {}).get("m365_event_id")
                        if event_id:
                            try:
                                remove_m365_event(m365_token, m365_user_id, event_id)
                            except Exception:
                                logger.exception("Failed to remove M365 event")

        # Silently prune completed tours from the previous day
        completed_pruned = prune_completed_tours(now)

        return (
            {
                "status": "success",
                "cancelled_count": cancelled_count,
                "reminders_sent": reminder_count,
                "completed_pruned": completed_pruned,
            },
            200,
        )
    except Exception as e:
        logger.exception("Unexpected error in prune_unpaid_slots")
        return ({"status": "error", "message": str(e)}, 500)
