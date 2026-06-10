import pytest
from unittest.mock import patch, MagicMock
import html
import sys
import os

# Ensure the app imports work smoothly
sys.path.insert(0, ".")

import main
import prune_unpaid_slots


@patch("requests.post")
def test_inject_m365_event_escaping(mock_post):
    # Set up mock response
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "test_event_id"}
    mock_post.return_value = mock_resp

    # Input with malicious HTML
    malicious_input = "<script>alert('xss')</script>"
    guest_data = {"name": malicious_input, "phone": "555-1234", "party_size": 4}

    # Call the function
    main.db.__class__.__name__ = "NotDummy"  # Force execution beyond Dummy check
    event_id = main.inject_m365_event(
        access_token="test_token",
        user_id="test_user",
        date_str="2026-06-15",
        time_str="10:00",
        guest_data=guest_data,
        booking_id="booking_<script>",
    )

    assert event_id == "test_event_id"

    # Inspect event payload sent to Microsoft Graph
    args, kwargs = mock_post.call_args
    payload = kwargs["json"]

    # Subject of the event (plain text)
    assert (
        payload["subject"] == f"[PENDING] Bodie Tour – {malicious_input} (Party of 4)"
    )

    # HTML body content should be escaped
    content = payload["body"]["content"]
    assert html.escape(malicious_input) in content
    assert html.escape("booking_<script>") in content
    assert "<script>" not in content


@patch("requests.post")
@patch("main.db")
@patch("main.get_m365_access_token")
def test_send_booking_receipt_email_escaping(mock_get_token, mock_db, mock_post):
    # Setup mocks
    mock_get_token.return_value = ("token", "user_id")

    mock_doc_get = MagicMock()
    mock_doc_get.exists = True
    mock_doc_get.to_dict.return_value = {
        "subject": "Receipt for {customer_name}",
        "body": "<p>Hello {customer_name}, your booking {booking_id} is confirmed.</p>",
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
        "token": "token123",
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


@patch("requests.post")
@patch("prune_unpaid_slots.db")
def test_send_outlook_reminder_escaping(mock_db, mock_post):
    mock_doc_get = MagicMock()
    mock_doc_get.exists = True
    mock_doc_get.to_dict.return_value = {
        "subject": "Reminder for {customer_name}",
        "body": "<p>Hello {customer_name}, your booking {booking_id} needs payment.</p>",
    }

    mock_doc = MagicMock()
    mock_doc.get.return_value = mock_doc_get
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_post.return_value = mock_resp

    malicious_name = "<i>Freya</i>"

    # Trigger with template_type custom
    with patch.dict("os.environ", {"EMAIL_TEMPLATE_TYPE": "custom"}):
        success = prune_unpaid_slots.send_outlook_reminder(
            access_token="token",
            user_id="user_id",
            customer_email="freya@example.com",
            customer_name=malicious_name,
            tour_datetime_str="2026-06-15 10:00 AM",
            booking_id="booking_<script>",
            payment_link="http://link",
            party_size=2,
            token="token",
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


def test_csrf_token_retrieval_and_cookie_generation():
    from flask import Request, Flask
    from werkzeug.test import EnvironBuilder

    app = Flask(__name__)
    with app.app_context():
        # 1. Test GET request on handle_booking returns 200, a CSRF token, and sets the matching HttpOnly cookie
        builder = EnvironBuilder(
            method="GET", headers={"Origin": "https://www.bodiefoundation.org"}
        )
        req = Request(builder.get_environ())

        response = main.handle_booking(req)
        # The return is a Flask response object
        assert response.status_code == 200
        json_data = response.get_json()
        assert json_data["status"] == "success"
        assert "csrf_token" in json_data
        token = json_data["csrf_token"]

        # Verify cookie was set
        cookie_header = response.headers.get("Set-Cookie")
        assert "csrf_token=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=None" in cookie_header
        assert "Secure" in cookie_header


def test_csrf_validation_fails_on_missing_mismatched():
    from flask import Request, Flask
    from werkzeug.test import EnvironBuilder

    # Force db class name to NOT contain Dummy/Mock/Proxy so that CSRF validation is active
    main.db.__class__.__name__ = "RealProductionClient"

    app = Flask(__name__)
    with app.app_context():
        try:
            # Test 1: POST request without CSRF cookie or header fails with 400
            builder = EnvironBuilder(
                method="POST", json={"date": "2026-06-15", "time": "10:00"}
            )
            req = Request(builder.get_environ())

            response, status_code, headers = main.handle_booking(req)
            assert status_code == 400
            assert response["status"] == "error"
            assert "CSRF verification failed" in response["message"]

            # Test 2: POST request with cookie but mismatched header fails with 400
            builder = EnvironBuilder(
                method="POST",
                json={"date": "2026-06-15", "time": "10:00"},
                headers={"X-CSRF-Token": "wrong_token"},
            )
            req = Request(builder.get_environ())
            # Manually inject the cookie
            req.cookies = {"csrf_token": "right_token"}

            response, status_code, headers = main.handle_booking(req)
            assert status_code == 400
            assert response["status"] == "error"
            assert "CSRF verification failed" in response["message"]
        finally:
            # Restore mock state
            main.db.__class__.__name__ = "MagicMock"


def test_strict_redirect_uri_validation():
    # Test that unauthorized redirect_uri raises ValueError
    auth_doc_invalid = {
        "environment": "production",
        "client_id": "cid",
        "client_secret": "csec",
        "callback_url": "https://attacker-site.com/steal-token",
    }
    with pytest.raises(ValueError) as excinfo:
        main._resolve_qbo_credentials(auth_doc_invalid)
    assert "Unauthorized QBO redirect_uri" in str(excinfo.value)

    # Test that whitelisted redirect_uri succeeds
    auth_doc_valid = {
        "environment": "production",
        "client_id": "cid",
        "client_secret": "csec",
        "callback_url": "https://us-west2-bodie-tours-prod.cloudfunctions.net/qbo-callback",
    }
    cid, sec, vt, cb = main._resolve_qbo_credentials(auth_doc_valid)
    assert cb == "https://us-west2-bodie-tours-prod.cloudfunctions.net/qbo-callback"
