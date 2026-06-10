import pytest
from unittest.mock import patch, MagicMock
import html
import sys
import os

# Ensure the app imports work smoothly
sys.path.insert(0, '.')

import main
import prune_unpaid_slots

@patch('requests.post')
def test_inject_m365_event_escaping(mock_post):
    # Set up mock response
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "test_event_id"}
    mock_post.return_value = mock_resp

    # Input with malicious HTML
    malicious_input = "<script>alert('xss')</script>"
    guest_data = {
        "name": malicious_input,
        "phone": "555-1234",
        "party_size": 4
    }

    # Call the function
    main.db.__class__.__name__ = "NotDummy" # Force execution beyond Dummy check
    event_id = main.inject_m365_event(
        access_token="test_token",
        user_id="test_user",
        date_str="2026-06-15",
        time_str="10:00",
        guest_data=guest_data,
        booking_id="booking_<script>"
    )

    assert event_id == "test_event_id"

    # Inspect event payload sent to Microsoft Graph
    args, kwargs = mock_post.call_args
    payload = kwargs["json"]

    # Subject of the event (plain text)
    assert payload["subject"] == f"[PENDING] Bodie Tour – {malicious_input} (Party of 4)"

    # HTML body content should be escaped
    content = payload["body"]["content"]
    assert html.escape(malicious_input) in content
    assert html.escape("booking_<script>") in content
    assert "<script>" not in content


@patch('requests.post')
@patch('main.db')
@patch('main.get_m365_access_token')
def test_send_booking_receipt_email_escaping(mock_get_token, mock_db, mock_post):
    # Setup mocks
    mock_get_token.return_value = ("token", "user_id")
    
    mock_doc_get = MagicMock()
    mock_doc_get.exists = True
    mock_doc_get.to_dict.return_value = {
        "subject": "Receipt for {customer_name}",
        "body": "<p>Hello {customer_name}, your booking {booking_id} is confirmed.</p>"
    }
    
    mock_doc = MagicMock()
    mock_doc.get.return_value = mock_doc_get
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_post.return_value = mock_resp

    # Customer name with HTML tags
    malicious_name = "<b>Freya</b>"
    booking_data = {
        "guest": {"name": malicious_name, "email": "freya@example.com"},
        "tour_datetime": "2026-06-15T10:00:00Z",
        "party_size": 2,
        "token": "token123"
    }

    main.db.__class__.__name__ = "NotDummy"
    success = main.send_booking_receipt_email("book_<script>", booking_data)
    assert success is True

    # Check payload
    args, kwargs = mock_post.call_args
    message_payload = kwargs["json"]["message"]

    # Subject should be unescaped plain-text
    assert message_payload["subject"] == f"Receipt for {malicious_name}"

    # Body should have escaped name
    assert html.escape(malicious_name) in message_payload["body"]["content"]
    assert "<b>" not in message_payload["body"]["content"]


@patch('requests.post')
@patch('prune_unpaid_slots.db')
def test_send_outlook_reminder_escaping(mock_db, mock_post):
    mock_doc_get = MagicMock()
    mock_doc_get.exists = True
    mock_doc_get.to_dict.return_value = {
        "subject": "Reminder for {customer_name}",
        "body": "<p>Hello {customer_name}, your booking {booking_id} needs payment.</p>"
    }
    
    mock_doc = MagicMock()
    mock_doc.get.return_value = mock_doc_get
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_post.return_value = mock_resp

    malicious_name = "<i>Freya</i>"
    
    # Trigger with template_type custom
    with patch.dict('os.environ', {'EMAIL_TEMPLATE_TYPE': 'custom'}):
        success = prune_unpaid_slots.send_outlook_reminder(
            access_token="token",
            user_id="user_id",
            customer_email="freya@example.com",
            customer_name=malicious_name,
            tour_datetime_str="2026-06-15 10:00 AM",
            booking_id="booking_<script>",
            payment_link="http://link",
            party_size=2,
            token="token"
        )
    assert success is True

    # Check payload
    args, kwargs = mock_post.call_args
    message_payload = kwargs["json"]["message"]

    # Subject should be plain text (unescaped)
    assert message_payload["subject"] == f"Reminder for {malicious_name}"

    # Body should have HTML escaped customer name
    assert html.escape(malicious_name) in message_payload["body"]["content"]
    assert "<i>" not in message_payload["body"]["content"]
