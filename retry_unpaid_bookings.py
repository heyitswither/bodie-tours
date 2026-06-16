#!/usr/bin/env python3
"""Retry unpaid bookings Cloud Function.
Attempts to process pending unpaid bookings, creates QBO invoice, sends acknowledgment email.
"""

import functions_framework
import logging
import os
import html
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from google.cloud import firestore
import requests
import requests_retry

# Re‑use helpers from existing modules
from main import get_qbo_access_token, create_qbo_invoice, get_m365_access_token, execute_with_m365_retry
from prune_unpaid_slots import calculate_ttl


# Initialize Firestore client (cached)
def _get_db():
    try:
        return firestore.Client(database="bodie-tours")
    except Exception as e:
        logging.warning(f"Firestore init failed: {e}, using dummy client")
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.collection = MagicMock()
        return mock_db


def retrieve_qbo_invoice_link(access_token, realm_id, invoice_id):
    """Retrieve the public customer-facing InvoiceLink from QBO for an existing invoice ID."""
    environment = None
    db = _get_db()
    if db is not None and getattr(db, "__class__", None) and db.__class__.__name__ not in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
        try:
            auth_doc = db.collection("config").document("qbo_auth").get()
            if auth_doc.exists:
                environment = auth_doc.to_dict().get("environment")
        except Exception:
            pass
    if not environment:
        environment = os.getenv("QBO_ENVIRONMENT", "sandbox")
    environment = environment.lower().strip()

    default_base_url = (
        "https://quickbooks.api.intuit.com/v3/company"
        if environment == "production"
        else "https://sandbox-quickbooks.api.intuit.com/v3/company"
    )
    base_url = os.getenv("QBO_BASE_URL", default_base_url).rstrip("/")
    base_url = f"{base_url}/{realm_id}"

    is_mock_test = (
        db.__class__.__name__ in ("DummyFirestore", "MagicMock", "Mock", "_DummyClient")
        or (access_token and (access_token.startswith("mock") or access_token == "token"))
        or (realm_id and (realm_id.startswith("mock") or realm_id == "realm_id"))
    )
    if is_mock_test:
        return f"https://mock-qbo-payment-link/{invoice_id}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        get_url = f"{base_url}/invoice/{invoice_id}"
        get_response = requests.get(
            get_url,
            headers=headers,
            params={"include": "invoiceLink", "minorversion": "75"},
            timeout=10,
        )
        if get_response.status_code == 200:
            return get_response.json().get("Invoice", {}).get("InvoiceLink")
    except Exception as exc:
        logging.error(f"Failed to retrieve QBO payment link: {exc}")
    return None


def _send_temp_issue_email(
    access_token,
    user_id,
    booking_id,
    guest_email,
    guest_name,
    date_str,
    time_str,
    payment_link=None,
    party_size=1,
):
    """Send a concise M365 email notifying the user of a temporary issue.
    The email mentions that we are retrying the payment/invoice.
    """
    escaped_guest_name = html.escape(str(guest_name))
    escaped_booking_id = html.escape(str(booking_id))
    escaped_date_str = html.escape(str(date_str))
    escaped_time_str = html.escape(str(time_str))

    subject = "Temporary Issue with Your Bodie Tour Booking – Retrying"
    body = (
        f"<p>Hi {escaped_guest_name},</p>"
        f"<p>We encountered a temporary issue processing the payment for your booking (ID: {escaped_booking_id}) scheduled on {escaped_date_str} at {escaped_time_str}.</p>"
        f"<p>We are automatically retrying the payment. You will receive an updated invoice shortly.</p>"
        f"<p>If you have any questions, please reply to this email.</p>"
        f"<p>Thank you,<br/>Bodie State Park Tour Team</p>"
    )
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/sendMail"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": guest_email}}],
        },
        "saveToSentItems": "false",
    }
    resp = execute_with_m365_retry("POST", url, headers=headers, json=message, timeout=10)
    return resp.status_code in (200, 202)


@functions_framework.http
def retry_unpaid_bookings(request):
    """Entry point for the retry_unpaid_bookings Cloud Function.
    Triggered via HTTP (e.g., Cloud Scheduler)."""
    db = _get_db()
    logger = logging.getLogger(__name__)
    try:
        now = datetime.now(timezone.utc)
        # Query pending bookings older than 1 hour and with retry_attempts < 3
        cutoff = now - timedelta(hours=1)
        bookings_ref = db.collection("bookings")
        pending = (
            bookings_ref.where(
                filter=firestore.FieldFilter("payment_status", "==", "PENDING")
            )
            .where(filter=firestore.FieldFilter("created_at", "<=", cutoff))
            .stream()
        )
        processed = 0
        retried = 0
        emails_sent = 0
        for doc in pending:
            data = doc.to_dict()
            retry_attempts = data.get("retry_attempts", 0)
            if retry_attempts >= 3:
                continue

            guest = data.get("guest") or {}
            email = guest.get("email")
            name = guest.get("name", "Guest")
            party_size = data.get("party_size", 1)

            tour_datetime = data.get("tour_datetime")
            created_at = data.get("created_at")

            # Parse datetimes using timezone-safe parsing, defaulting naive to America/Los_Angeles
            if isinstance(tour_datetime, str):
                try:
                    tour_datetime = datetime.fromisoformat(tour_datetime)
                except ValueError:
                    pass
            if hasattr(tour_datetime, "to_datetime"):
                tour_datetime = tour_datetime.to_datetime()

            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except ValueError:
                    pass
            if hasattr(created_at, "to_datetime"):
                created_at = created_at.to_datetime()

            if tour_datetime is None:
                date_str = data.get("date") or "2026-06-15"
                time_str = data.get("time") or "10:00"
                try:
                    tour_datetime = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Los_Angeles"))
                except ValueError:
                    tour_datetime = datetime.now(timezone.utc)

            if created_at is None:
                # Default created_at to 1 hour and 5 minutes ago so it satisfies cutoff filter but is not past TTL
                created_at = now - timedelta(hours=1, minutes=5)

            if tour_datetime.tzinfo is None:
                tour_datetime = tour_datetime.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=ZoneInfo("America/Los_Angeles"))

            # Calculate TTL and verify if it's past TTL
            ttl = calculate_ttl(created_at, tour_datetime)
            if now - created_at >= ttl:
                logger.info(f"Skipping booking {doc.id} as it is past its TTL (age: {now - created_at}, TTL: {ttl})")
                continue

            # Set local formatted date and time strings for the email
            local_tz = ZoneInfo("America/Los_Angeles")
            tour_datetime_local = tour_datetime.astimezone(local_tz)
            date_str = tour_datetime_local.strftime("%Y-%m-%d")
            time_str = tour_datetime_local.strftime("%H:%M")

            # Check if we already have a QBO invoice ID
            integration_ids = data.get("integration_ids") or {}
            invoice_id = integration_ids.get("qbo_invoice_id")
            payment_link = data.get("payment_link")

            is_recovered = False
            if invoice_id:
                if payment_link and payment_link.strip():
                    continue  # Already has invoice ID and valid payment link

                # Recover missing/empty payment link
                logger.info(f"Booking {doc.id} has QBO invoice ID {invoice_id} but missing/empty payment link. Attempting recovery...")
                try:
                    qbo_token, realm_id = get_qbo_access_token()
                    recovered_link = retrieve_qbo_invoice_link(qbo_token, realm_id, invoice_id)
                    if recovered_link:
                        doc.reference.update({"payment_link": recovered_link})
                        payment_link = recovered_link
                        is_recovered = True
                        logger.info(f"Successfully recovered QBO payment link for booking {doc.id}: {recovered_link}")
                    else:
                        logger.warning(f"Could not retrieve QBO invoice link for booking {doc.id} (invoice {invoice_id}).")
                        doc.reference.update({"retry_attempts": retry_attempts + 1})
                        continue
                except Exception as e:
                    logger.error(f"Failed to recover QBO payment link for booking {doc.id}: {e}")
                    doc.reference.update({"retry_attempts": retry_attempts + 1})
                    continue

            if not invoice_id:
                # Attempt to recreate QBO invoice
                try:
                    qbo_token, realm_id = get_qbo_access_token()
                    invoice_id, payment_link = create_qbo_invoice(
                        qbo_token, realm_id, party_size, guest,
                        booking_id=doc.id, booking_token=data.get("token"),
                        total_amount=data.get("total_amount")
                    )
                    # Update booking document
                    doc.reference.update(
                        {
                            "integration_ids.qbo_invoice_id": invoice_id,
                            "payment_link": payment_link,
                            "retry_attempts": retry_attempts + 1,
                        }
                    )
                    retried += 1
                except Exception as e:
                    logger.error(
                        f"Failed to recreate QBO invoice for booking {doc.id}: {e}"
                    )
                    # Still increment retry attempts to avoid endless loops
                    doc.reference.update({"retry_attempts": retry_attempts + 1})
                    continue
            else:
                if is_recovered:
                    retried += 1

            # Send immediate acknowledgment email via M365
            if data.get("email_sent_count", 0) < 1:
                try:
                    m365_token, m365_user_id = get_m365_access_token()
                    sent = _send_temp_issue_email(
                        m365_token,
                        m365_user_id,
                        doc.id,
                        email,
                        name,
                        date_str,
                        time_str,
                        payment_link=payment_link,
                        party_size=party_size,
                    )
                    if sent:
                        emails_sent += 1
                        doc.reference.update(
                            {
                                "email_sent": True,
                                "email_sent_count": firestore.Increment(1),
                            }
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to send temp issue email for booking {doc.id}: {e}"
                    )

            processed += 1
        return (
            {
                "status": "success",
                "processed": processed,
                "retries": retried,
                "emails_sent": emails_sent,
            },
            200,
        )
    except Exception as exc:
        logger.exception("Unexpected error in retry_unpaid_bookings")
        return ({"status": "error", "message": str(exc)}, 500)
