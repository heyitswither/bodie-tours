import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Ensure google.cloud.firestore is mocked before import
if "google.cloud.firestore" in sys.modules:
    mock_firestore = sys.modules["google.cloud.firestore"]
else:
    mock_firestore = MagicMock()
    mock_firestore.transactional = lambda f: f
    sys.modules["google.cloud.firestore"] = mock_firestore

import main
from datetime import datetime, timezone, timedelta
from flask import Request, jsonify
from werkzeug.test import EnvironBuilder
import json
import base64


@pytest.fixture
def mock_main_db():
    with patch("main.db") as mock_db:
        mock_query = MagicMock()
        mock_db.collection.return_value.where.return_value = mock_query
        mock_query.where.return_value = mock_query
        yield mock_db


# ===========================================================================
# 1. m365_free_availability Tests
# ===========================================================================


@patch("main.get_m365_access_token")
@patch("requests.get")
def test_m365_free_availability_branches(mock_get, mock_token, mock_main_db):
    mock_token.return_value = ("token", "ranger_1")

    # 1. Non-GET request -> 405
    builder = EnvironBuilder(method="POST")
    request = Request(builder.get_environ())
    body, status = main.m365_free_availability(request)
    assert status == 405

    # 2. GET request success with calendar events
    builder = EnvironBuilder(
        method="GET", query_string="start=2026-06-15&end=2026-06-15"
    )
    request = Request(builder.get_environ())

    # Mock calendarView response with Windows / PST timeZone name
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "value": [
            {
                "subject": "Touring Hours",
                "showAs": "free",
                "start": {
                    "dateTime": "2026-06-15T09:00:00",
                    "timeZone": "Pacific Standard Time",
                },
                "end": {
                    "dateTime": "2026-06-15T10:00:00",
                    "timeZone": "Pacific Standard Time",
                },
            }
        ]
    }
    mock_get.return_value = mock_response

    # Mock public inventory
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "taken_slots": [
            datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)
        ]  # 16:00 UTC = 09:00 PDT
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_doc
    )

    # Use standard client class name to enter normal DB branch
    mock_client_db = MagicMock()
    mock_client_db.__class__.__name__ = "FirestoreClient"
    mock_client_db.collection = mock_main_db.collection

    with patch("main.db", mock_client_db):
        resp, status = main.m365_free_availability(request)
        assert status == 200
        # Parse from Flask response or direct dict if returned as dict
        if isinstance(resp, dict):
            data = resp
        else:
            data = json.loads(resp.get_data(as_text=True))
        assert "dates" in data

    # 3. Exception in main.m365_free_availability -> 500
    mock_token.side_effect = Exception("Auth Down")
    resp, status = main.m365_free_availability(request)
    assert status == 500


# ===========================================================================
# 2. cancel_tour Tests
# ===========================================================================


@patch("main.requests.delete")
def test_cancel_tour_branches(mock_delete, mock_main_db):
    # 1. OPTIONS request -> 204
    builder = EnvironBuilder(
        method="OPTIONS", headers={"Origin": "https://www.bodiefoundation.org"}
    )
    request = Request(builder.get_environ())
    body, status, headers = main.cancel_tour(request)
    assert status == 204

    # 2. GET request without booking_id or token -> 400
    builder = EnvironBuilder(method="GET", query_string="")
    request = Request(builder.get_environ())
    body, status, headers = main.cancel_tour(request)
    assert status == 400

    # 3. Booking not found -> 404
    builder = EnvironBuilder(method="GET", query_string="booking_id=b_123&token=t_abc")
    request = Request(builder.get_environ())
    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = False
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_booking_snap
    )
    body, status, headers = main.cancel_tour(request)
    assert status == 404

    # 4. Valid cancellation
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {
        "payment_status": "PENDING",
        "token": "t_abc",
        "party_size": 2,
        "tour_datetime": "2026-06-15T10:00:00Z",
        "integration_ids": {"m365_event_id": "event_123", "qbo_invoice_id": "inv_123"},
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_booking_snap
    )

    # Mock token validation and M365 delete success
    with patch("main.get_m365_access_token", return_value=("tok", "usr")):
        mock_delete.return_value.status_code = 204
        body, status, headers = main.cancel_tour(request)
        assert status == 200
        assert body["status"] == "success"


# ===========================================================================
# 3. qbo_webhook Tests
# ===========================================================================


@patch("main.db")
def test_qbo_webhook_branches(mock_db):
    # 1. OPTIONS request -> 405 (Method Not Allowed since Webhook is only POST)
    builder = EnvironBuilder(method="OPTIONS")
    request = Request(builder.get_environ())
    body, status = main.qbo_webhook(request)
    assert status == 405

    # 2. Missing signature -> 401
    builder = EnvironBuilder(method="POST", data=json.dumps({"test": "data"}))
    request = Request(builder.get_environ())
    body, status = main.qbo_webhook(request)
    assert status == 401

    # 3. Valid signature, processes invoice paid
    payload = {
        "eventNotifications": [
            {
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "id": "invoice_123", "operation": "Update"}
                    ]
                }
            }
        ]
    }
    payload_str = json.dumps(payload)

    # Mock verifier_token inside config/qbo_auth
    verifier_token = "my_verifier_token"
    mock_auth_doc = MagicMock()
    mock_auth_doc.to_dict.return_value = {"verifier_token": verifier_token}
    mock_db.collection.return_value.document.return_value.get.return_value = (
        mock_auth_doc
    )

    # Compute correct Intuit-Signature header
    import hmac
    import hashlib

    computed = hmac.new(
        verifier_token.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256
    ).digest()
    signature = base64.b64encode(computed).decode("utf-8")

    # Mock bookings query match
    mock_booking_doc = MagicMock()
    mock_booking_doc.id = "booking_123"
    mock_booking_doc.to_dict.return_value = {
        "payment_status": "PENDING",
        "guest": {"name": "Alice", "email": "alice@example.com"},
    }
    mock_db.collection.return_value.where.return_value.stream.return_value = [
        mock_booking_doc
    ]

    builder = EnvironBuilder(
        method="POST",
        headers={"Intuit-Signature": signature},
        data=payload_str,
        content_type="application/json",
    )
    request = Request(builder.get_environ())

    with patch("main.send_booking_receipt_email", return_value=True) as mock_receipt:
        body, status = main.qbo_webhook(request)
        assert status == 200
        assert body["status"] == "success"
        mock_receipt.assert_called_once()


# ===========================================================================
# 4. send_booking_receipt_email Tests
# ===========================================================================


@patch("main.get_m365_access_token")
@patch("requests.post")
def test_send_booking_receipt_email_branches(mock_post, mock_token, mock_main_db):
    mock_token.return_value = ("m_token", "m_user")

    # 1. Success sending with custom template from Firestore
    mock_tmpl_doc = MagicMock()
    mock_tmpl_doc.exists = True
    mock_tmpl_doc.to_dict.return_value = {
        "subject": "receipt {{booking_id}}",
        "body": "Hi {{customer_name}}, total: {{total_amount}}",
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_tmpl_doc
    )

    mock_post.return_value.status_code = 202
    booking_data = {
        "payment_status": "PAID",
        "tour_datetime": "2026-06-15T10:00:00Z",
        "party_size": 2,
        "payment_link": "http://pay",
        "guest": {"name": "Alice", "email": "alice@example.com"},
    }

    # Use standard client class name to enter normal DB branch
    mock_client_db = MagicMock()
    mock_client_db.__class__.__name__ = "FirestoreClient"
    mock_client_db.collection = mock_main_db.collection

    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data)
        assert res is True

    # 2. Template doc does not exist fallback to default body
    mock_tmpl_doc.exists = False
    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data)
        assert res is True

    # 3. Token fetch failure -> False
    mock_token.side_effect = Exception("Token failed")
    res = main.send_booking_receipt_email("booking_123", booking_data)
    assert res is False


# ===========================================================================
# 5. send_booking_receipt_email Additional Gaps (Lines 278-279, 285-286, 289, 338-339, 397-398)
# ===========================================================================


@patch("main.get_m365_access_token")
@patch("requests.post")
def test_send_booking_receipt_email_additional_gaps(
    mock_post, mock_token, mock_main_db
):
    mock_token.return_value = ("m_token", "m_user")

    # Use standard client class name to enter normal DB branch
    mock_client_db = MagicMock()
    mock_client_db.__class__.__name__ = "FirestoreClient"
    mock_client_db.collection = mock_main_db.collection

    # 1. Missing customer_email (lines 278-279)
    booking_data_no_email = {"guest": {}}  # empty guest
    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data_no_email)
        assert res is False

    # 2. Missing tour_datetime (lines 285-286)
    booking_data_no_dt = {"guest": {"name": "Alice", "email": "alice@example.com"}}
    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data_no_dt)
        assert res is False

    # 3. Naive tour_datetime (line 289)
    booking_data_naive_dt = {
        "guest": {"name": "Alice", "email": "alice@example.com"},
        "tour_datetime": "2026-06-15T10:00:00",  # naive
        "party_size": 2,
    }
    mock_tmpl_doc = MagicMock()
    mock_tmpl_doc.exists = True
    mock_tmpl_doc.to_dict.return_value = {"subject": "confirmed", "body": "hi"}
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_tmpl_doc
    )
    mock_post.return_value.status_code = 202

    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data_naive_dt)
        assert res is True

    # 4. Template Firestore fetch exception (lines 338-339)
    mock_main_db.collection.return_value.document.return_value.get.side_effect = (
        Exception("Firestore read error")
    )
    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data_naive_dt)
        assert res is True  # Falls back to default template and returns True

    # 5. POST requests fails with 400 (lines 397-398)
    mock_main_db.collection.return_value.document.return_value.get.side_effect = None
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_tmpl_doc
    )
    mock_post.return_value.status_code = 400
    with patch("main.db", mock_client_db):
        res = main.send_booking_receipt_email("booking_123", booking_data_naive_dt)
        assert res is False

    # 6. DummyFirestore branch (lines 398-400)
    mock_post.return_value.status_code = 202
    mock_client_db_dummy = MagicMock()
    mock_client_db_dummy.__class__.__name__ = "DummyFirestore"
    mock_client_db_dummy.collection = mock_main_db.collection
    with patch("main.db", mock_client_db_dummy):
        res = main.send_booking_receipt_email("booking_123", booking_data_naive_dt)
        assert res is True


# ===========================================================================
# 6. _resolve_qbo_credentials & get_m365_access_token (Lines 404-405, 458)
# ===========================================================================


def test_resolve_qbo_credentials_none():
    # Empty auth_data (lines 404-405)
    cid, _, _, _ = main._resolve_qbo_credentials(None)
    assert cid is None


@patch("main.db")
def test_get_m365_access_token_missing_credentials(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {}  # empty dict
    mock_db.collection.return_value.document.return_value = mock_doc

    with patch.dict(os.environ, {}), patch("os.environ.get", return_value=None):
        with pytest.raises(Exception, match="M365 Auth configuration missing."):
            main.get_m365_access_token()


# ===========================================================================
# 7. qbo_login & qbo_callback (Lines 808-809, 830-831, 836, 843-846, 849-850, 855-863)
# ===========================================================================


@patch("main.db")
def test_qbo_login_config_exception(mock_db):
    # getting config/qbo_auth fails (lines 808-809)
    mock_db.collection.return_value.document.return_value.get.side_effect = Exception(
        "Db error"
    )
    builder = EnvironBuilder(method="GET")
    request = Request(builder.get_environ())
    resp = main.qbo_login(request)
    assert (
        resp.status_code == 302
    )  # Redirect still succeeds using environment fallbacks


@patch("main.db")
@patch("requests.post")
def test_qbo_callback_failures(mock_post, mock_db):
    # 1. Missing state or verifier_token (lines 830-831)
    builder = EnvironBuilder(method="GET", query_string="code=123")
    request = Request(builder.get_environ())
    body, status = main.qbo_callback(request)
    assert status == 400

    # 2. State mismatch (line 836)
    builder = EnvironBuilder(
        method="GET",
        query_string="code=123&realmId=456&state=state_abc",
        headers={"Cookie": "qbo_oauth_state=state_different"},
    )
    request = Request(builder.get_environ())
    body, status = main.qbo_callback(request)
    assert status == 400

    # Setup for successful state matching but failing token endpoints
    mock_auth_doc = MagicMock()
    mock_auth_doc.get.return_value.to_dict.return_value = {
        "client_id": "cid",
        "client_secret": "csec",
        "redirect_uri": "http://callback",
    }
    mock_db.collection.return_value.document.return_value = mock_auth_doc

    # 3. Token endpoint response failure (lines 843-846)
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad request token exchange"
    import requests

    mock_resp.raise_for_status.side_effect = requests.HTTPError(
        "Bad request token exchange"
    )
    mock_post.return_value = mock_resp

    builder = EnvironBuilder(
        method="GET",
        query_string="code=123&realmId=456&state=state_abc",
        headers={"Cookie": "qbo_oauth_state=state_abc"},
    )
    request = Request(builder.get_environ())
    body, status = main.qbo_callback(request)
    assert status == 500
    assert "Bad request token exchange" in body["message"]

    # 4. RealmId or refresh_token not in payload (lines 849-850)
    mock_resp_success = MagicMock()
    mock_resp_success.status_code = 200
    mock_resp_success.json.return_value = {
        "access_token": "tok"
    }  # missing refresh_token
    mock_post.return_value = mock_resp_success
    body, status = main.qbo_callback(request)
    assert status == 500
    assert "realmId or refresh_token missing in response" in body["message"]

    # 5. Exception handler block (lines 855-863)
    mock_post.side_effect = Exception("Network timeout")
    body, status = main.qbo_callback(request)
    assert status == 500
    assert "Network timeout" in body["message"]


# ===========================================================================
# 8. m365_login (Lines 995, 997)
# ===========================================================================


@patch("main.db")
def test_m365_login_branches(mock_db):
    mock_auth_doc = MagicMock()
    mock_auth_doc.get.return_value.to_dict.return_value = {
        "client_id": "cid",
        "redirect_uri": "http://callback",
    }
    mock_db.collection.return_value.document.return_value = mock_auth_doc

    builder = EnvironBuilder(method="GET")
    request = Request(builder.get_environ())
    resp = main.m365_login(request)
    assert resp.status_code == 302


# ===========================================================================
# 9. qbo_webhook Additional Gaps (Lines 1178-1179, 1204-1206, 1211, 1216-1217)
# ===========================================================================


@patch("main.db")
def test_qbo_webhook_additional_gaps(mock_db):
    # 1. Invalid verifier_token type (lines 1178-1179)
    mock_auth_doc = MagicMock()
    mock_auth_doc.exists = True
    # verifier_token is None or empty
    mock_auth_doc.to_dict.return_value = {"dev-verifier_token": None}
    mock_db.collection.return_value.document.return_value.get.return_value = (
        mock_auth_doc
    )

    builder = EnvironBuilder(method="POST", headers={"Intuit-Signature": "sig"})
    request = Request(builder.get_environ())
    body, status = main.qbo_webhook(request)
    assert status == 401
    assert "verifier_token" in body

    # 2. Verifier token as bytes (line 1211) & computed signature mismatch (lines 1216-1217)
    mock_auth_doc.to_dict.return_value = {"dev-verifier_token": b"my_bytes_verifier"}
    body, status = main.qbo_webhook(request)
    assert status == 401  # Signature mismatch because signature header is random

    # 3. Empty or invalid JSON payload (lines 1204-1206)
    # Using correct signature but empty payload
    verifier_token = "my_token"
    mock_auth_doc.to_dict.return_value = {"dev-verifier_token": verifier_token}
    import hmac, hashlib, base64

    computed = hmac.new(verifier_token.encode("utf-8"), b"", hashlib.sha256).digest()
    signature = base64.b64encode(computed).decode("utf-8")

    builder = EnvironBuilder(
        method="POST",
        headers={"Intuit-Signature": signature},
        data="",  # empty
        content_type="application/json",
    )
    request = Request(builder.get_environ())
    body, status = main.qbo_webhook(request)
    assert status == 400


# ===========================================================================
# 10. m365_free_availability & cancel_tour Gaps (Lines 1242-1243, 1311, 1317, 1328-1331)
# ===========================================================================


@patch("main.get_m365_access_token")
@patch("requests.get")
def test_m365_free_availability_exception_branches(mock_get, mock_token, mock_main_db):
    mock_token.return_value = ("token", "ranger_1")
    builder = EnvironBuilder(
        method="GET", query_string="start=2026-06-15&end=2026-06-15"
    )
    request = Request(builder.get_environ())

    # Graph requests.get raises exception (lines 1242-1243)
    mock_get.side_effect = Exception("Microsoft Down")

    mock_doc = MagicMock()
    mock_doc.exists = False
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_doc
    )

    resp, status = main.m365_free_availability(request)
    assert (
        status == 200
    )  # Returns empty results since it catches and falls back to empty events


@patch("main.requests.delete")
def test_cancel_tour_additional_gaps(mock_delete, mock_main_db):
    # 1. Naive tour_datetime in cancel_tour (line 1311)
    builder = EnvironBuilder(method="GET", query_string="booking_id=b_123&token=t_abc")
    request = Request(builder.get_environ())

    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {
        "payment_status": "PENDING",
        "token": "t_abc",
        "party_size": 2,
        "tour_datetime": "2026-06-15T10:00:00",  # naive
        "integration_ids": {"m365_event_id": "event_123"},
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_booking_snap
    )
    mock_delete.return_value.status_code = 204

    with patch("main.get_m365_access_token", return_value=("tok", "usr")):
        body, status, headers = main.cancel_tour(request)
        assert status == 200

    # 2. inventory_ref.update raises an exception (line 1317)
    def update_side_effect(arg):
        if "payment_status" in arg:
            return None
        raise Exception("Db write limit")

    mock_main_db.collection.return_value.document.return_value.update.side_effect = (
        update_side_effect
    )
    with patch("main.get_m365_access_token", return_value=("tok", "usr")):
        body, status, headers = main.cancel_tour(request)
        assert (
            status == 200
        )  # Still returns 200 since the inventory release exception is caught and ignored

    # 3. Unhandled exception returns 500 (lines 1328-1331)
    mock_booking_snap.to_dict.side_effect = Exception("Unexpected mapping error")
    body, status, headers = main.cancel_tour(request)
    assert status == 500
    assert "Unexpected mapping error" in body["message"]


# ===========================================================================
# 11. check_m365_availability & inject_m365_event (Lines 174, 180, 250, 252)
# ===========================================================================


@patch("requests.get")
def test_check_m365_availability_direct(mock_get, mock_main_db):
    mock_client_db = MagicMock()
    mock_client_db.__class__.__name__ = "FirestoreClient"
    mock_client_db.collection = mock_main_db.collection

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "value": [
            {
                "subject": "Touring Hours",
                "showAs": "free",
                "start": {"dateTime": "2026-06-15T16:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-15T19:00:00", "timeZone": "UTC"},
            }
        ]
    }
    mock_get.return_value = mock_response

    with patch("main.db", mock_client_db):
        # 1. With calendar_id (covers line 174)
        res = main.check_m365_availability(
            "token", "user_1", "2026-06-15", "10:00", "cal_123"
        )
        assert res is True
        args, kwargs = mock_get.call_args
        assert "calendars/cal_123" in args[0]

        # 2. Without calendar_id (covers line 180)
        res = main.check_m365_availability(
            "token", "user_1", "2026-06-15", "10:00", None
        )
        assert res is True
        args, kwargs = mock_get.call_args
        assert "calendars" not in args[0]


@patch("requests.post")
def test_inject_m365_event_direct(mock_post, mock_main_db):
    mock_client_db = MagicMock()
    mock_client_db.__class__.__name__ = "FirestoreClient"
    mock_client_db.collection = mock_main_db.collection

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": "event_999"}
    mock_post.return_value = mock_response

    guest_data = {"name": "Alice", "phone": "123"}

    with patch("main.db", mock_client_db):
        # 1. With calendar_id (covers line 250)
        eid = main.inject_m365_event(
            "token", "user_1", "2026-06-15", "10:00", guest_data, "booking_1", "cal_123"
        )
        assert eid == "event_999"
        args, kwargs = mock_post.call_args
        assert "calendars/cal_123" in args[0]

        # 2. Without calendar_id (covers line 252)
        eid = main.inject_m365_event(
            "token", "user_1", "2026-06-15", "10:00", guest_data, "booking_1", None
        )
        assert eid == "event_999"
        args, kwargs = mock_post.call_args
        assert "calendars" not in args[0]


# ===========================================================================
# 12. More main.py Gaps (Lines 480, 1022, 1024, 1231, 1238, 1338)
# ===========================================================================


def test_resolve_qbo_credentials_production_missing_redirect_uri():
    # Production with missing redirect_uri (line 480)
    auth_data = {
        "environment": "production",
        "prod-id": "prod_id",
        "prod-secret": "prod_sec",
        "prod-verifier_token": "verifier",
    }
    with patch.dict(os.environ, {}, clear=True), patch(
        "os.environ.get", return_value=None
    ):
        with pytest.raises(
            ValueError,
            match="Redirect URI must be configured for QBO in production environment",
        ):
            main._resolve_qbo_credentials(auth_data)


@patch("main.db")
def test_m365_login_missing_config(mock_db):
    # missing config causes M365 client_id missing (lines 1022-1024)
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {}  # empty dict
    mock_db.collection.return_value.document.return_value = mock_doc

    with patch.dict(os.environ, {}, clear=True), patch(
        "os.environ.get", return_value=None
    ):
        builder = EnvironBuilder(method="GET")
        request = Request(builder.get_environ())
        body, status = main.m365_login(request)
        assert status == 500
        assert "M365 client_id is not configured" in body["message"]


@patch("main.get_m365_access_token")
@patch("requests.get")
def test_m365_free_availability_legacy_slots_and_timestamps(
    mock_get, mock_token, mock_main_db
):
    mock_token.return_value = ("token", "ranger_1")
    builder = EnvironBuilder(
        method="GET", query_string="start=2026-06-15&end=2026-06-15"
    )
    request = Request(builder.get_environ())

    # Mock calendarView response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"value": []}
    mock_get.return_value = mock_response

    # 1. Test legacy slots dictionary fallback (lines 1231-1233)
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "slots": {
            "10:00": {"taken": 2, "status": "SOLD_OUT"},
            "11:00": {"taken": 0, "status": "AVAILABLE"},  # not booked
        }
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_doc
    )

    mock_client_db = MagicMock()
    mock_client_db.__class__.__name__ = "FirestoreClient"
    mock_client_db.collection = mock_main_db.collection

    with patch("main.db", mock_client_db):
        resp, status = main.m365_free_availability(request)
        assert status == 200

    # 2. Test Firestore Timestamp objects converting to_datetime() (line 1238)
    mock_ts = MagicMock()
    mock_ts.to_datetime.return_value = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    mock_doc.to_dict.return_value = {"slots": [mock_ts]}  # list of timestamps
    with patch("main.db", mock_client_db):
        resp, status = main.m365_free_availability(request)
        assert status == 200


def test_cancel_tour_invalid_token_branch(mock_main_db):
    # Invalid token mismatch (line 1338)
    builder = EnvironBuilder(
        method="GET", query_string="booking_id=b_123&token=token_mismatch"
    )
    request = Request(builder.get_environ())

    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {
        "payment_status": "PENDING",
        "token": "token_correct",
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = (
        mock_booking_snap
    )

    body, status, headers = main.cancel_tour(request)
    assert status == 403
    assert "Invalid token" in body["message"]


# ===========================================================================
# 13. Naive Timestamps Cached Tokens & Missing Public Document
# ===========================================================================


@patch("main.db")
def test_get_m365_access_token_naive_cached(mock_db):
    mock_doc = MagicMock()
    naive_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger_1",
        "access_token": "valid_cached",
        "expires_at": naive_expires,
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    token, user_id = main.get_m365_access_token()
    assert token == "valid_cached"
    assert user_id == "ranger_1"


@patch("main.db")
def test_get_qbo_access_token_naive_cached(mock_db):
    mock_doc = MagicMock()
    naive_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    mock_doc.get.return_value.to_dict.return_value = {
        "access_token": "qbo_cached",
        "expires_at": naive_expires,
        "realmId": "realm_123",
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    token, realm_id = main.get_qbo_access_token()
    assert token == "qbo_cached"
    assert realm_id == "realm_123"


def test_process_booking_transaction_document_not_exists():
    transaction = MagicMock()
    inventory_ref = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = False
    inventory_ref.get.return_value = mock_snapshot

    booking_id = main.process_booking_transaction(
        transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"}
    )
    assert booking_id is not None
