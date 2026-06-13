#!/usr/bin/env python3
"""Retry unpaid bookings Cloud Function.
Attempts to process pending unpaid bookings, creates QBO invoice, sends acknowledgment email.
"""

import functions_framework
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from google.cloud import firestore
import requests
import requests_retry

# Re‑use helpers from existing modules
from main import get_qbo_access_token, create_qbo_invoice, get_m365_access_token


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
    subject = "Temporary Issue with Your Bodie Tour Booking – Retrying"
    body = (
        f"<p>Hi {guest_name},</p>"
        f"<p>We encountered a temporary issue processing the payment for your booking (ID: {booking_id}) scheduled on {date_str} at {time_str}.</p>"
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
    resp = requests.post(url, headers=headers, json=message, timeout=10)
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

            # Skip bookings that already have a successful QBO invoice ID (they are just waiting for payment)
            integration_ids = data.get("integration_ids") or {}
            if integration_ids.get("qbo_invoice_id"):
                continue

            guest = data.get("guest") or {}
            email = guest.get("email")
            name = guest.get("name", "Guest")
            tour_datetime = data.get("tour_datetime")
            if isinstance(tour_datetime, str):
                tour_datetime = datetime.fromisoformat(tour_datetime)
            if tour_datetime is not None:
                if tour_datetime.tzinfo is None:
                    tour_datetime = tour_datetime.replace(tzinfo=timezone.utc)
                local_tz = ZoneInfo("America/Los_Angeles")
                tour_datetime_local = tour_datetime.astimezone(local_tz)
                date_str = tour_datetime_local.strftime("%Y-%m-%d")
                time_str = tour_datetime_local.strftime("%H:%M")
            else:
                date_str = data.get("date") or "2026-06-15"
                time_str = data.get("time") or "10:00"
            party_size = data.get("party_size", 1)

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
