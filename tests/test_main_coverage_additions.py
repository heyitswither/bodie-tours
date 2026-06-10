import sys
from unittest.mock import MagicMock
mock_firestore = MagicMock()
mock_firestore.transactional = lambda f: f
import types
sys.modules['google.cloud.firestore'] = mock_firestore
if 'google.cloud' in sys.modules:
    sys.modules['google.cloud'].firestore = mock_firestore
else:
    pkg = types.ModuleType('google.cloud')
    pkg.__path__ = []
    pkg.firestore = mock_firestore
    sys.modules['google.cloud'] = pkg
sys.path.insert(0, '.')

import pytest
from unittest.mock import patch, MagicMock, ANY
from flask import Request
from werkzeug.test import EnvironBuilder
import datetime
from datetime import timezone, timedelta
import os
import hmac
import hashlib
import base64
import json
import requests
import main

@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {
        'QBO_CLIENT_ID': 'test_client_id',
        'QBO_CLIENT_SECRET': 'test_client_secret',
        'QBO_REDIRECT_URI': 'https://example.com/callback',
        'QBO_ENVIRONMENT': 'sandbox'
    }):
        yield

def make_mock_post_request(headers=None, json_data=None):
    data_str = json.dumps(json_data) if json_data is not None else None
    builder = EnvironBuilder(
        method='POST',
        headers=headers or {},
        data=data_str,
        content_type='application/json'
    )
    return Request(builder.get_environ())

# ===========================================================================
# TC-M01 to TC-M06: get_m365_access_token
# ===========================================================================

@patch('main.db')
def test_tc_m01_get_m365_token_missing_auth(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = None
    mock_db.collection.return_value.document.return_value = mock_doc
    
    with pytest.raises(Exception, match="M365 Auth configuration missing."):
        main.get_m365_access_token()

@patch('main.db')
def test_tc_m02_get_m365_token_missing_userid(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "access_token": "some_token",
        "expires_at": datetime.datetime.now(timezone.utc) + timedelta(hours=1)
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    with pytest.raises(RuntimeError, match="M365 Auth configuration missing user_id."):
        main.get_m365_access_token()

@patch('main.db')
@patch('requests.post')
def test_tc_m03_get_m365_token_expired_cached(mock_post, mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger123",
        "access_token": "old_token",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new_token",
        "refresh_token": "new_rtoken",
        "expires_in": 3600
    }
    mock_post.return_value = mock_response
    
    token, user_id = main.get_m365_access_token()
    assert token == "new_token"
    assert user_id == "ranger123"
    
    mock_doc.update.assert_called_once()
    update_data = mock_doc.update.call_args[0][0]
    assert update_data["access_token"] == "new_token"
    assert update_data["refresh_token"] == "new_rtoken"

@patch('main.db')
@patch('requests.post')
def test_tc_m04_get_m365_token_http_failure(mock_post, mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger123",
        "access_token": "old_token",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    mock_post.return_value = mock_response
    
    with pytest.raises(Exception, match="Failed to refresh M365 token: Bad Request"):
        main.get_m365_access_token()

@patch('main.db')
@patch('requests.post')
def test_tc_m05_get_m365_token_missing_token_in_json(mock_post, mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger123",
        "access_token": "old_token",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "refresh_token": "new_rtoken"
    }
    mock_post.return_value = mock_response
    
    with pytest.raises(Exception, match="Could not obtain access token from response."):
        main.get_m365_access_token()

@patch('main.db')
@patch('requests.post')
def test_tc_m06_get_m365_token_no_new_refresh_token(mock_post, mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger123",
        "access_token": "old_token",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new_token"
    }
    mock_post.return_value = mock_response
    
    token, user_id = main.get_m365_access_token()
    assert token == "new_token"
    update_data = mock_doc.update.call_args[0][0]
    assert "refresh_token" not in update_data

@patch('main.db')
@patch('requests.post')
def test_tc_m06_get_m365_token_same_refresh_token(mock_post, mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger123",
        "access_token": "old_token",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new_token",
        "refresh_token": "old_rtoken"
    }
    mock_post.return_value = mock_response
    
    token, user_id = main.get_m365_access_token()
    assert token == "new_token"
    update_data = mock_doc.update.call_args[0][0]
    assert "refresh_token" not in update_data

@patch('main.db')
def test_get_m365_token_valid_cached(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger123",
        "access_token": "valid_token",
        "expires_at": datetime.datetime.now(timezone.utc) + timedelta(hours=1),
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    token, user_id = main.get_m365_access_token()
    assert token == "valid_token"
    assert user_id == "ranger123"

# ===========================================================================
# TC-M07 to TC-M10: check_m365_availability
# ===========================================================================

@patch('main.db')
@patch('requests.get')
def test_tc_m07_check_m365_availability_http_failure(mock_get, mock_db):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Error"
    mock_get.return_value = mock_response
    
    with pytest.raises(Exception, match="Failed to query M365 calendar: Internal Error"):
        main.check_m365_availability("token", "user_id", "2026-06-15", "10:00")

@patch('main.db')
@patch('requests.get')
def test_tc_m08_check_m365_availability_mismatched_criteria(mock_get, mock_db):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "value": [
            {"subject": "Lunch Break", "showAs": "free", "start": {"dateTime": "2026-06-15T10:00:00", "timeZone": "Pacific Standard Time"}, "end": {"dateTime": "2026-06-15T11:00:00", "timeZone": "Pacific Standard Time"}},
            {"subject": "Touring Hours", "showAs": "busy", "start": {"dateTime": "2026-06-15T10:00:00", "timeZone": "Pacific Standard Time"}, "end": {"dateTime": "2026-06-15T11:00:00", "timeZone": "Pacific Standard Time"}}
        ]
    }
    mock_get.return_value = mock_response
    
    assert main.check_m365_availability("token", "user_id", "2026-06-15", "10:00") is False

@patch('main.db')
@patch('requests.get')
def test_tc_m09_check_m365_availability_tz_fallbacks(mock_get, mock_db):
    # Case 1: Empty tz_name -> UTC
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "value": [
            {
                "subject": "Touring Hours Part 1",
                "showAs": "free",
                "start": {"dateTime": "2026-06-15T17:00:00", "timeZone": None},
                "end": {"dateTime": "2026-06-15T18:00:00", "timeZone": None}
            }
        ]
    }
    mock_get.return_value = mock_response
    assert main.check_m365_availability("token", "user_id", "2026-06-15", "10:00") is True

    # Case 2: Invalid tz_name -> UTC
    mock_response.json.return_value = {
        "value": [
            {
                "subject": "Touring Hours Part 2",
                "showAs": "free",
                "start": {"dateTime": "2026-06-15T17:00:00", "timeZone": "Invalid/TZ"},
                "end": {"dateTime": "2026-06-15T18:00:00", "timeZone": "Invalid/TZ"}
            }
        ]
    }
    assert main.check_m365_availability("token", "user_id", "2026-06-15", "10:00") is True

    # Case 3: Valid non-PST zone (e.g., UTC)
    mock_response.json.return_value = {
        "value": [
            {
                "subject": "Touring Hours Part 3",
                "showAs": "free",
                "start": {"dateTime": "2026-06-15T17:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-15T18:00:00", "timeZone": "UTC"}
            }
        ]
    }
    assert main.check_m365_availability("token", "user_id", "2026-06-15", "10:00") is True

@patch('main.db')
@patch('requests.get')
def test_tc_m10_check_m365_availability_partial_span(mock_get, mock_db):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "value": [
            {
                "subject": "Touring Hours",
                "showAs": "free",
                "start": {"dateTime": "2026-06-15T10:00:00", "timeZone": "Pacific Standard Time"},
                "end": {"dateTime": "2026-06-15T10:45:00", "timeZone": "Pacific Standard Time"}
            }
        ]
    }
    mock_get.return_value = mock_response
    assert main.check_m365_availability("token", "user_id", "2026-06-15", "10:00") is False

# ===========================================================================
# TC-M11: inject_m365_event
# ===========================================================================

@patch('main.db')
@patch('requests.post')
def test_tc_m11_inject_m365_event_failure(mock_post, mock_db):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Injection Error"
    mock_post.return_value = mock_response
    
    with pytest.raises(Exception, match="Failed to inject M365 event: Injection Error"):
        main.inject_m365_event(
            "token", "user_id", "2026-06-15", "10:00",
            {"name": "Alice", "phone": "123", "party_size": 2}, "booking_id"
        )

# ===========================================================================
# TC-M12 to TC-M15: get_qbo_access_token
# ===========================================================================

@patch('main.db')
def test_tc_m12_get_qbo_token_missing_auth(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = None
    mock_db.collection.return_value.document.return_value = mock_doc
    
    with pytest.raises(Exception, match="QBO Auth configuration missing. Run OAuth flow first."):
        main.get_qbo_access_token()

@patch('main.db')
@patch('requests.post')
def test_tc_m13_get_qbo_token_expired(mock_post, mock_db, mock_env):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "access_token": "old_qbo",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "refresh_token": "old_qbo_refresh",
        "realmId": "qbo_realm_123"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "new_qbo_token",
        "refresh_token": "new_qbo_refresh",
        "expires_in": 3600
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response
    
    token, realm_id = main.get_qbo_access_token()
    assert token == "new_qbo_token"
    assert realm_id == "qbo_realm_123"
    
    mock_doc.update.assert_called_once()
    update_data = mock_doc.update.call_args[0][0]
    assert update_data["access_token"] == "new_qbo_token"
    assert update_data["refresh_token"] == "new_qbo_refresh"

@patch('main.db')
def test_get_qbo_token_valid_cached(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "access_token": "valid_qbo",
        "expires_at": datetime.datetime.now(timezone.utc) + timedelta(hours=1),
        "realmId": "qbo_realm_123"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    token, realm_id = main.get_qbo_access_token()
    assert token == "valid_qbo"
    assert realm_id == "qbo_realm_123"

@patch('main.db')
def test_tc_m14_get_qbo_token_missing_credentials(mock_db):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "access_token": "old_qbo",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "refresh_token": "old_qbo_refresh",
        "realmId": "qbo_realm_123"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(Exception, match="Missing QBO credentials for token refresh."):
            main.get_qbo_access_token()

@patch('main.db')
@patch('requests.post')
def test_tc_m15_get_qbo_token_no_new_refresh(mock_post, mock_db, mock_env):
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = {
        "access_token": "old_qbo",
        "expires_at": datetime.datetime.now(timezone.utc) - timedelta(hours=1),
        "refresh_token": "old_qbo_refresh",
        "realmId": "qbo_realm_123"
    }
    mock_db.collection.return_value.document.return_value = mock_doc
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "new_qbo_token",
        "expires_in": 3600
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response
    
    token, realm_id = main.get_qbo_access_token()
    assert token == "new_qbo_token"
    update_data = mock_doc.update.call_args[0][0]
    assert "refresh_token" not in update_data

# ===========================================================================
# TC-M16 to TC-M17: create_qbo_invoice
# ===========================================================================

@patch('requests.post')
def test_tc_m16_create_qbo_invoice_production(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "Invoice": {
            "Id": "prod_inv_123"
        }
    }
    mock_post.return_value = mock_response
    
    with patch.dict(os.environ, {"QBO_ENVIRONMENT": "production"}):
        invoice_id, payment_link = main.create_qbo_invoice(
            "token", "realm_id", 4, {"email": "cust@example.com"}
        )
        assert invoice_id == "prod_inv_123"
        assert payment_link == "https://app.qbo.intuit.com/app/invoice?txnId=prod_inv_123"
        
        args, kwargs = mock_post.call_args
        assert args[0].startswith("https://quickbooks.api.intuit.com/v3/company/realm_id") or args[0].startswith("https://sandbox-quickbooks.api.intuit.com/v3/company/realm_id")

@patch('requests.post')
def test_create_qbo_invoice_sandbox(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "Invoice": {
            "Id": "sandbox_inv_123"
        }
    }
    mock_post.return_value = mock_response
    
    with patch.dict(os.environ, {"QBO_ENVIRONMENT": "sandbox"}):
        invoice_id, payment_link = main.create_qbo_invoice(
            "token", "realm_id", 4, {"email": "cust@example.com"}
        )
        assert invoice_id == "sandbox_inv_123"
        assert payment_link == "https://app.sandbox.qbo.intuit.com/app/invoice?txnId=sandbox_inv_123"
        
        args, kwargs = mock_post.call_args
        assert args[0].startswith("https://sandbox-quickbooks.api.intuit.com/v3/company/realm_id")

@patch('requests.post')
def test_tc_m17_create_qbo_invoice_failure(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Invalid customer"
    mock_post.return_value = mock_response
    
    with pytest.raises(Exception, match="Failed to create QBO invoice: Invalid customer"):
        main.create_qbo_invoice("token", "realm_id", 4, {"email": "cust@example.com"})

# ===========================================================================
# TC-M18 to TC-M20: process_booking_transaction
# ===========================================================================

def test_tc_m18_process_booking_group_size_too_large():
    with pytest.raises(ValueError, match="Maximum group size is 20."):
        main.process_booking_transaction(
            MagicMock(), MagicMock(), "2026-06-15", "10:00", 21, {"name": "Bob"}
        )

@patch('main.db')
def test_tc_m19_process_booking_inventory_exists_not_booked(mock_db):
    transaction = MagicMock()
    inventory_ref = MagicMock()
    
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {
        "taken_slots": []
    }
    inventory_ref.get.return_value = mock_snapshot
    
    booking_id = main.process_booking_transaction(
        transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"}
    )
    
    assert booking_id is not None
    transaction.set.assert_any_call(inventory_ref, ANY, merge=True)

@patch('main.db')
def test_tc_m20_process_booking_slot_already_booked(mock_db):
    transaction = MagicMock()
    inventory_ref = MagicMock()
    
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    local_tz = ZoneInfo("America/Los_Angeles")
    dt_local = datetime.strptime("2026-06-15 10:00", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    dt_local_utc = dt_local.astimezone(timezone.utc)
    
    mock_snapshot.to_dict.return_value = {
        "taken_slots": [dt_local_utc]
    }
    inventory_ref.get.return_value = mock_snapshot
    
    with pytest.raises(ValueError, match="This time slot is already booked by another group."):
        main.process_booking_transaction(
            transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"}
        )

# ===========================================================================
# TC-M21 to TC-M27: handle_booking
# ===========================================================================

def test_tc_m21_handle_booking_cors_origins():
    origins = [
        "https://bodiefoundation.org",
        "https://www.bodiefoundation.org",
        "https://site.squarespace.com",
        "http://localhost:3000",
        "http://127.0.0.1:8080"
    ]
    
    for origin in origins:
        req = make_mock_post_request(
            headers={"Origin": origin},
            json_data={"date": "invalid-date"}
        )
        response, status_code, resp_headers = main.handle_booking(req)
        assert status_code == 409
        assert resp_headers.get("Access-Control-Allow-Origin") == origin
        
    req = make_mock_post_request(
        headers={"Origin": "https://malicious.com"},
        json_data={"date": "invalid-date"}
    )
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 409
    assert resp_headers.get("Access-Control-Allow-Origin") == "https://www.bodiefoundation.org"

def test_tc_m22_handle_booking_options_preflight():
    builder = EnvironBuilder(
        method='OPTIONS',
        headers={"Origin": "https://bodiefoundation.org"}
    )
    req = Request(builder.get_environ())
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 204
    assert response == ''
    assert resp_headers.get("Access-Control-Allow-Origin") == "https://bodiefoundation.org"
    assert resp_headers.get("Access-Control-Allow-Methods") == "POST"

def test_tc_m23_handle_booking_invalid_date_format():
    req = make_mock_post_request(
        json_data={"date": "2026/06/15"}
    )
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 409
    assert response["message"] == "Invalid date format. Expected YYYY-MM-DD."

def test_tc_m24_handle_booking_invalid_time_format():
    req = make_mock_post_request(
        json_data={"date": "2026-06-15", "time": "10-00"}
    )
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 409
    assert response["message"] == "Invalid time format. Expected HH:MM."

@patch('main.db')
@patch('main.get_m365_access_token')
@patch('main.check_m365_availability')
@patch('main.process_booking_transaction')
@patch('main.get_qbo_access_token')
def test_tc_m25_handle_booking_rollback_success(
    mock_qbo_token, mock_process_booking, mock_m365_avail, mock_m365_token, mock_db
):
    mock_m365_token.return_value = ("m365_token", "ranger_id")
    mock_m365_avail.return_value = True
    mock_process_booking.return_value = "booking_123"
    mock_qbo_token.side_effect = Exception("QBO API Down")
    
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    local_tz = ZoneInfo("America/Los_Angeles")
    dt_local = datetime.strptime("2026-06-15 10:00", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    dt_local_utc = dt_local.astimezone(timezone.utc)
    
    mock_inventory_snapshot = MagicMock()
    mock_inventory_snapshot.exists = True
    mock_inventory_snapshot.to_dict.return_value = {
        "taken_slots": [dt_local_utc]
    }
    
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.get.return_value = mock_inventory_snapshot
    
    mock_booking_doc = MagicMock()
    
    def collection_side_effect(name):
        mock_col = MagicMock()
        if name == "public":
            mock_col.document.return_value = mock_inventory_ref
        elif name == "bookings":
            mock_col.document.return_value = mock_booking_doc
        return mock_col
        
    mock_db.collection.side_effect = collection_side_effect
    
    mock_transaction = MagicMock()
    mock_transaction.get.return_value = mock_inventory_snapshot
    mock_db.transaction.return_value = mock_transaction
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice", "email": "alice@example.com"}
        }
    )
    
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 500
    assert response["message"] == "Failed to process payload."
    
    # Check that bookings doc is deleted
    mock_booking_doc.delete.assert_called_once()
    
    # Check that rollback updated inventory
    mock_inventory_ref.update.assert_called_once()
    update_data = mock_inventory_ref.update.call_args[0][0]
    assert update_data["taken_slots"] == []

@patch('main.db')
@patch('main.get_m365_access_token')
@patch('main.check_m365_availability')
@patch('main.process_booking_transaction')
@patch('main.get_qbo_access_token')
def test_tc_m25_handle_booking_rollback_fails(
    mock_qbo_token, mock_process_booking, mock_m365_avail, mock_m365_token, mock_db
):
    mock_m365_token.return_value = ("m365_token", "ranger_id")
    mock_m365_avail.return_value = True
    mock_process_booking.return_value = "booking_123"
    mock_qbo_token.side_effect = Exception("QBO API Down")
    
    mock_booking_doc = MagicMock()
    mock_booking_doc.delete.side_effect = Exception("Firestore Delete Error")
    
    def collection_side_effect(name):
        mock_col = MagicMock()
        if name == "bookings":
            mock_col.document.return_value = mock_booking_doc
        return mock_col
        
    mock_db.collection.side_effect = collection_side_effect
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice", "email": "alice@example.com"}
        }
    )
    
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 500
    assert response["message"] == "Failed to process payload."

@patch('main.db')
@patch('main.get_m365_access_token')
@patch('main.check_m365_availability')
@patch('main.process_booking_transaction')
@patch('main.get_qbo_access_token')
def test_tc_m25_handle_booking_rollback_snapshot_not_exists(
    mock_qbo_token, mock_process_booking, mock_m365_avail, mock_m365_token, mock_db
):
    mock_m365_token.return_value = ("m365_token", "ranger_id")
    mock_m365_avail.return_value = True
    mock_process_booking.return_value = "booking_123"
    mock_qbo_token.side_effect = Exception("QBO API Down")
    
    mock_inventory_snapshot = MagicMock()
    mock_inventory_snapshot.exists = False
    
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.get.return_value = mock_inventory_snapshot
    
    mock_booking_doc = MagicMock()
    
    def collection_side_effect(name):
        mock_col = MagicMock()
        if name == "public":
            mock_col.document.return_value = mock_inventory_ref
        elif name == "bookings":
            mock_col.document.return_value = mock_booking_doc
        return mock_col
        
    mock_db.collection.side_effect = collection_side_effect
    
    mock_transaction = MagicMock()
    mock_transaction.get.return_value = mock_inventory_snapshot
    mock_db.transaction.return_value = mock_transaction
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice", "email": "alice@example.com"}
        }
    )
    
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 500
    mock_transaction.set.assert_not_called()

@patch('main.db')
@patch('main.get_m365_access_token')
@patch('main.check_m365_availability')
@patch('main.process_booking_transaction')
@patch('main.get_qbo_access_token')
def test_tc_m25_handle_booking_rollback_time_not_in_slots(
    mock_qbo_token, mock_process_booking, mock_m365_avail, mock_m365_token, mock_db
):
    mock_m365_token.return_value = ("m365_token", "ranger_id")
    mock_m365_avail.return_value = True
    mock_process_booking.return_value = "booking_123"
    mock_qbo_token.side_effect = Exception("QBO API Down")
    
    mock_inventory_snapshot = MagicMock()
    mock_inventory_snapshot.exists = True
    mock_inventory_snapshot.to_dict.return_value = {
        "taken_slots": []
    }
    
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.get.return_value = mock_inventory_snapshot
    
    mock_booking_doc = MagicMock()
    
    def collection_side_effect(name):
        mock_col = MagicMock()
        if name == "public":
            mock_col.document.return_value = mock_inventory_ref
        elif name == "bookings":
            mock_col.document.return_value = mock_booking_doc
        return mock_col
        
    mock_db.collection.side_effect = collection_side_effect
    
    mock_transaction = MagicMock()
    mock_transaction.get.return_value = mock_inventory_snapshot
    mock_db.transaction.return_value = mock_transaction
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice", "email": "alice@example.com"}
        }
    )
    
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 500
    mock_transaction.set.assert_not_called()

@patch('main.get_m365_access_token')
@patch('main.check_m365_availability')
@patch('main.process_booking_transaction')
def test_tc_m26_handle_booking_value_error(mock_process, mock_avail, mock_m365_token):
    mock_m365_token.return_value = ("token", "user")
    mock_avail.return_value = True
    mock_process.side_effect = ValueError("This slot is already booked")
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice"}
        }
    )
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 409
    assert response["message"] == "This slot is already booked"

@patch('main.get_m365_access_token')
def test_tc_m27_handle_booking_general_exception(mock_m365_token):
    mock_m365_token.side_effect = Exception("Uncaught database failure")
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice"}
        }
    )
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 500
    assert response["message"] == "Failed to process payload."

@patch('main.db')
@patch('main.get_m365_access_token')
@patch('main.check_m365_availability')
@patch('main.process_booking_transaction')
@patch('main.get_qbo_access_token')
@patch('main.create_qbo_invoice')
@patch('main.inject_m365_event')
def test_handle_booking_success(
    mock_inject, mock_create_inv, mock_qbo_token, mock_process_booking, mock_m365_avail, mock_m365_token, mock_db
):
    mock_m365_token.return_value = ("m365_token", "ranger_id")
    mock_m365_avail.return_value = True
    mock_process_booking.return_value = "booking_123"
    mock_qbo_token.return_value = ("qbo_token", "realm_id")
    mock_create_inv.return_value = ("invoice_999", "https://payment-link.com")
    mock_inject.return_value = "event_888"
    
    mock_booking_doc = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_booking_doc
    
    req = make_mock_post_request(
        json_data={
            "date": "2026-06-15",
            "time": "10:00",
            "party_size": 4,
            "guest": {"name": "Alice", "email": "alice@example.com"}
        }
    )
    
    response, status_code, resp_headers = main.handle_booking(req)
    assert status_code == 200
    assert response["status"] == "success"
    assert response["booking_id"] == "booking_123"
    assert response["payment_link"] == "https://payment-link.com"
    
    mock_booking_doc.update.assert_called_once_with({
        "integration_ids.qbo_invoice_id": "invoice_999",
        "integration_ids.m365_event_id": "event_888",
        "payment_link": "https://payment-link.com"
    })

# ===========================================================================
# TC-M28 to TC-M29: qbo_callback (database error)
# ===========================================================================

@patch('main.db')
@patch('requests.post')
def test_tc_m29_qbo_callback_db_error(mock_post, mock_db, mock_env):
    builder = EnvironBuilder(
        method='GET',
        query_string={'code': 'test_auth_code', 'realmId': '123456', 'state': 'qbo_auth_state_xyz123'}
    )
    req = Request(builder.get_environ())
    req.cookies = {'qbo_oauth_state': 'qbo_auth_state_xyz123'}

    mock_response = MagicMock()
    mock_response.json.return_value = {
        'access_token': 'test_access_token',
        'refresh_token': 'test_refresh_token',
        'expires_in': 3600
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    mock_db.collection.return_value.document.return_value.set.side_effect = Exception("Firestore set error")

    response, status_code = main.qbo_callback(req)
    assert status_code == 500
    assert response['status'] == 'error'
    assert 'An error occurred' in response['message']

# ===========================================================================
# TC-M30 to TC-M35: m365_login and m365_callback
# ===========================================================================

@patch('main.db')
def test_tc_m30_m365_login(mock_db):
    mock_db.collection.return_value.document.return_value.get.return_value.to_dict.return_value = {
        "tenant_id": "my_tenant",
        "client_id": "my_client"
    }
    
    with patch.dict(os.environ, {"M365_REDIRECT_URI": "https://callback.com"}):
        req = Request(EnvironBuilder(method='GET').get_environ())
        response = main.m365_login(req)
        assert response.status_code == 302
        assert response.location.startswith("https://login.microsoftonline.com/my_tenant/oauth2/v2.0/authorize?")
        assert "client_id=my_client" in response.location
        assert "redirect_uri=https%3A%2F%2Fcallback.com" in response.location

def test_tc_m31_m365_callback_invalid_state():
    builder = EnvironBuilder(method='GET', query_string={"code": "123", "state": "stateA"})
    req = Request(builder.get_environ())
    req.cookies = {"m365_oauth_state": "stateB"}
    response, status_code = main.m365_callback(req)
    assert status_code == 400
    assert response["message"] == "Invalid state parameter"
    
    builder = EnvironBuilder(method='GET', query_string={"code": "123", "state": "stateA"})
    req = Request(builder.get_environ())
    response, status_code = main.m365_callback(req)
    assert status_code == 400
    assert response["message"] == "Invalid state parameter"

def test_tc_m32_m365_callback_missing_code():
    builder = EnvironBuilder(method='GET', query_string={"state": "stateA"})
    req = Request(builder.get_environ())
    req.cookies = {"m365_oauth_state": "stateA"}
    response, status_code = main.m365_callback(req)
    assert status_code == 400
    assert response["message"] == "Missing authorization code."

@patch('main.db')
@patch('requests.post')
def test_tc_m33_m365_callback_success(mock_post, mock_db):
    mock_db.collection.return_value.document.return_value.get.return_value.to_dict.return_value = {
        "tenant_id": "my_tenant",
        "client_id": "my_client",
        "client_secret": "my_secret"
    }
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "new_m365_token",
        "refresh_token": "new_m365_refresh",
        "expires_in": 3600
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response
    
    builder = EnvironBuilder(method='GET', query_string={"code": "auth_code", "state": "stateA"})
    req = Request(builder.get_environ())
    req.cookies = {"m365_oauth_state": "stateA"}
    
    mock_doc = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc
    mock_doc.get.return_value.to_dict.return_value = {
        "tenant_id": "my_tenant",
        "client_id": "my_client"
    }
    
    response, status_code = main.m365_callback(req)
    assert status_code == 200
    assert response["status"] == "success"
    
    mock_doc.update.assert_called_once()
    update_data = mock_doc.update.call_args[0][0]
    assert update_data["access_token"] == "new_m365_token"
    assert update_data["refresh_token"] == "new_m365_refresh"

@patch('main.db')
@patch('requests.post')
def test_tc_m34_m365_callback_http_error(mock_post, mock_db):
    mock_post.side_effect = requests.exceptions.RequestException("Microsoft token endpoint down")
    
    builder = EnvironBuilder(method='GET', query_string={"code": "auth_code", "state": "stateA"})
    req = Request(builder.get_environ())
    req.cookies = {"m365_oauth_state": "stateA"}
    
    mock_db.collection.return_value.document.return_value.get.return_value.to_dict.return_value = {
        "tenant_id": "my_tenant",
        "client_id": "my_client"
    }
    
    response, status_code = main.m365_callback(req)
    assert status_code == 500
    assert "Failed to exchange M365 token:" in response["message"]

@patch('main.db')
@patch('requests.post')
def test_tc_m35_m365_callback_db_error(mock_post, mock_db):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "new_m365_token",
        "expires_in": 3600
    }
    mock_post.return_value = mock_response
    
    builder = EnvironBuilder(method='GET', query_string={"code": "auth_code", "state": "stateA"})
    req = Request(builder.get_environ())
    req.cookies = {"m365_oauth_state": "stateA"}
    
    mock_doc = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc
    mock_doc.get.return_value.to_dict.return_value = {
        "tenant_id": "my_tenant"
    }
    mock_doc.update.side_effect = Exception("Firestore update failed")
    
    response, status_code = main.m365_callback(req)
    assert status_code == 500
    assert "An error occurred:" in response["message"]

# ===========================================================================
# TC-M36 to TC-M43: qbo_webhook
# ===========================================================================

def test_tc_m36_qbo_webhook_invalid_method():
    builder = EnvironBuilder(method='GET')
    req = Request(builder.get_environ())
    response, status_code = main.qbo_webhook(req)
    assert status_code == 405
    assert response == 'Method Not Allowed'

@patch('main.db')
def test_tc_m37_qbo_webhook_missing_verifier_token(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = False
    mock_doc.get.return_value = mock_snapshot
    mock_db.collection.return_value.document.return_value = mock_doc
    
    payload = {
        "eventNotifications": [
            {
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "operation": "Update", "id": "invoice_123"}
                    ]
                }
            }
        ]
    }
    
    builder = EnvironBuilder(
        method='POST',
        data=json.dumps(payload),
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 401
    assert "Missing or invalid verifier_token" in response

@patch('main.db')
def test_tc_m38_qbo_webhook_invalid_base64_signature(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {"verifier_token": "some_token"}
    mock_doc.get.return_value = mock_snapshot
    mock_db.collection.return_value.document.return_value = mock_doc
    
    builder = EnvironBuilder(
        method='POST',
        headers={"Intuit-Signature": "!!!NotBase64!!!"},
        data=json.dumps({"some": "data"}),
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 401
    assert response == 'Unauthorized: Invalid Intuit-Signature encoding'

@patch('main.db')
def test_tc_m39_qbo_webhook_verifier_token_types(mock_db):
    payload = b'{"eventNotifications": []}'
    
    tokens_to_test = [
        "my_string_token",
        b"my_bytes_token",
        123456
    ]
    
    for token in tokens_to_test:
        mock_doc = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.exists = True
        mock_snapshot.to_dict.return_value = {"verifier_token": token}
        mock_doc.get.return_value = mock_snapshot
        mock_db.collection.return_value.document.return_value = mock_doc
        
        if isinstance(token, str):
            key = token.encode('utf-8')
        elif isinstance(token, bytes):
            key = token
        else:
            key = str(token).encode('utf-8')
            
        computed = hmac.new(key, payload, hashlib.sha256).digest()
        b64_sig = base64.b64encode(computed).decode('utf-8')
        
        builder = EnvironBuilder(
            method='POST',
            headers={"Intuit-Signature": b64_sig},
            data=payload,
            content_type='application/json'
        )
        req = Request(builder.get_environ())
        
        response, status_code = main.qbo_webhook(req)
        assert status_code == 200
        assert response == {"status": "success"}

@patch('main.db')
def test_qbo_webhook_mismatched_signature(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {"verifier_token": "token"}
    mock_doc.get.return_value = mock_snapshot
    mock_db.collection.return_value.document.return_value = mock_doc
    
    builder = EnvironBuilder(
        method='POST',
        headers={"Intuit-Signature": base64.b64encode(b"wrong_sig").decode('utf-8')},
        data=b'payload',
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 401
    assert response == 'Unauthorized: Invalid Intuit-Signature'

@patch('main.db')
def test_tc_m40_qbo_webhook_missing_payload(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {"verifier_token": "some_token"}
    mock_doc.get.return_value = mock_snapshot
    mock_db.collection.return_value.document.return_value = mock_doc
    
    payload = b''
    computed = hmac.new(b"some_token", payload, hashlib.sha256).digest()
    b64_sig = base64.b64encode(computed).decode('utf-8')

    builder = EnvironBuilder(
        method='POST',
        headers={"Intuit-Signature": b64_sig},
        data=payload,
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 400
    assert response == 'Bad Request'

@patch('main.db')
def test_tc_m41_qbo_webhook_ignore_non_invoice_and_ops(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {"verifier_token": "some_token"}
    mock_doc.get.return_value = mock_snapshot
    mock_db.collection.return_value.document.return_value = mock_doc
    
    payload = {
        "eventNotifications": [
            {
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Payment", "operation": "Update", "id": "123"},
                        {"name": "Invoice", "operation": "Delete", "id": "123"}
                    ]
                }
            }
        ]
    }
    
    payload_bytes = json.dumps(payload).encode('utf-8')
    computed = hmac.new(b"some_token", payload_bytes, hashlib.sha256).digest()
    b64_sig = base64.b64encode(computed).decode('utf-8')

    builder = EnvironBuilder(
        method='POST',
        headers={"Intuit-Signature": b64_sig},
        data=payload_bytes,
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 200
    assert response == {"status": "success"}
    
    for call in mock_db.collection.call_args_list:
        assert call[0][0] != "bookings"

@patch('main.db')
def test_tc_m42_qbo_webhook_missing_entity_id(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {"verifier_token": "some_token"}
    mock_doc.get.return_value = mock_snapshot
    mock_db.collection.return_value.document.return_value = mock_doc
    
    payload = {
        "eventNotifications": [
            {
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "operation": "Update"}
                    ]
                }
            }
        ]
    }
    
    payload_bytes = json.dumps(payload).encode('utf-8')
    computed = hmac.new(b"some_token", payload_bytes, hashlib.sha256).digest()
    b64_sig = base64.b64encode(computed).decode('utf-8')

    builder = EnvironBuilder(
        method='POST',
        headers={"Intuit-Signature": b64_sig},
        data=payload_bytes,
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 200
    assert response == {"status": "success"}
    for call in mock_db.collection.call_args_list:
        assert call[0][0] != "bookings"

@patch('main.db')
def test_tc_m43_qbo_webhook_general_exception(mock_db):
    mock_doc = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {"verifier_token": "some_token"}
    mock_doc.document.return_value.get.return_value = mock_snapshot
    
    def collection_side_effect(name):
        if name == "config":
            return mock_doc
        elif name == "bookings":
            raise Exception("Firestore query failed")
        return MagicMock()
        
    mock_db.collection.side_effect = collection_side_effect
    
    payload = {
        "eventNotifications": [
            {
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "operation": "Update", "id": "invoice_123"}
                    ]
                }
            }
        ]
    }
    
    payload_bytes = json.dumps(payload).encode('utf-8')
    computed = hmac.new(b"some_token", payload_bytes, hashlib.sha256).digest()
    b64_sig = base64.b64encode(computed).decode('utf-8')

    builder = EnvironBuilder(
        method='POST',
        headers={"Intuit-Signature": b64_sig},
        data=payload_bytes,
        content_type='application/json'
    )
    req = Request(builder.get_environ())
    
    response, status_code = main.qbo_webhook(req)
    assert status_code == 500
    assert response["status"] == "error"
    assert "Firestore query failed" in response["message"]

def test_firestore_client_initialization_failure():
    import importlib
    import main
    from unittest.mock import patch
    
    with patch("google.cloud.firestore.Client", side_effect=Exception("Initialization error")):
        try:
            importlib.reload(main)
            assert main.db.__class__.__name__ == "DummyFirestore"
            
            # Exercise DummyFirestore and dummy classes to get coverage on all their lines (23-50)
            dummy_col = main.db.collection("test_col")
            assert dummy_col.__class__.__name__ == "_DummyCollection"
            
            dummy_doc = dummy_col.document("test_doc")
            assert dummy_doc.__class__.__name__ == "_DummyDoc"
            
            # Test methods on _DummyDoc
            assert dummy_doc.get() == dummy_doc
            assert dummy_doc.to_dict() == {}
            dummy_doc.set({"a": 1})
            dummy_doc.update({"a": 2})
            dummy_doc.delete()
            assert dummy_doc.stream() == []
            
            # Test methods on _DummyCollection
            assert dummy_col.where() == dummy_col
            assert dummy_col.get() == []
        finally:
            # Re-reload main to restore the normal Firestore client db
            importlib.reload(main)

def test_firestore_client_dummy_db_full_coverage():
    import importlib
    import main
    import os
    from unittest.mock import MagicMock
    
    orig_env = os.environ.get("FORCE_DUMMY_DB")
    os.environ["FORCE_DUMMY_DB"] = "1"
    
    try:
        importlib.reload(main)
        assert main.db.__class__.__name__ == "DummyFirestore"
        
        # Mock requests.post for mock QBO invoice creation
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "Invoice": {
                "Id": "mock_qbo_invoice_id"
            }
        }
        with patch('requests.post', return_value=mock_response):
            # 1. DummyTransaction coverage (lines 53-54, 56, 58, 60, 65)
            tx = main.db.transaction()
            assert tx.__class__.__name__ == "DummyTransaction"
            assert tx._read_only is False
            assert tx._id == b"mock-id"
            tx.set()
            tx.update()
            tx.delete()
            
            # 2. Dummy checks in integrations (lines 77, 139, 200, 254, 314-316)
            m_tok, m_usr = main.get_m365_access_token()
            assert m_tok == "mock_m365_token"
            
            avail = main.check_m365_availability(m_tok, m_usr, "2026-06-15", "10:00")
            assert avail is True
            
            evt = main.inject_m365_event(m_tok, m_usr, "2026-06-15", "10:00", {}, "b_123")
            assert evt == "mock_m365_event_id"
            
            q_tok, realm = main.get_qbo_access_token()
            assert q_tok == "mock_qbo_token"
            
            # Standard invoice
            inv_id, link = main.create_qbo_invoice(q_tok, realm, 2, {"name": "Test"})
            assert inv_id == "mock_qbo_invoice_id"
            
            # Conflict Error invoice (lines 314-316)
            try:
                main.create_qbo_invoice(q_tok, realm, 2, {"name": "Conflict Error"})
            except ValueError as e:
                assert str(e) == "Booking conflict: The requested time slot is already taken."
                
            # Test handle_booking with dummy db
            req = MagicMock()
            req.method = 'POST'
            req.headers = {'Origin': 'http://localhost:8000'}
            req.get_json.return_value = {
                "date": "2026-06-15",
                "time": "10:00",
                "party_size": 2,
                "guest": {"name": "Test User", "email": "test@example.com"}
            }
            resp, code, headers = main.handle_booking(req)
            assert code == 200

            # Call qbo_login and qbo_callback under DummyFirestore db to hit False branches
            req_login = MagicMock()
            resp_login = main.qbo_login(req_login)
            assert resp_login.status_code == 302

            req_callback = MagicMock()
            req_callback.args = {'code': 'test_auth_code', 'realmId': '123456', 'state': 'qbo_state'}
            req_callback.cookies = {'qbo_oauth_state': 'qbo_state'}
            mock_post_cb = MagicMock()
            mock_post_cb.status_code = 200
            mock_post_cb.json.return_value = {
                'access_token': 'new_acc',
                'refresh_token': 'new_ref',
                'expires_in': 3600
            }
            with patch('requests.post', return_value=mock_post_cb):
                resp_cb, code_cb = main.qbo_callback(req_callback)
                assert code_cb == 200

        
    finally:
        if orig_env is None:
            del os.environ["FORCE_DUMMY_DB"]
        else:
            os.environ["FORCE_DUMMY_DB"] = orig_env
        importlib.reload(main)

def test_qbo_credentials_resolution_coverage():
    # 1. Environment is sandbox, dev-* are strings (True branches for sandbox, False branches for fallbacks)
    auth_sandbox = {
        "environment": "sandbox",
        "dev-id": "dev_id_val",
        "dev-secret": "dev_secret_val",
        "dev-verifier_token": "dev_verifier_token_val",
        "callback_url": "callback_url_val"
    }
    cid, sec, vt, cb = main._resolve_qbo_credentials(auth_sandbox)
    assert cid == "dev_id_val"
    assert sec == "dev_secret_val"
    assert vt == "dev_verifier_token_val"
    assert cb == "callback_url_val"

    # Test dev-verify fallback
    auth_sandbox_fallback = {
        "environment": "sandbox",
        "dev-id": "dev_id_val",
        "dev-secret": "dev_secret_val",
        "dev-verify": "dev_verify_val",
        "callback_url": "callback_url_val"
    }
    _, _, vt, _ = main._resolve_qbo_credentials(auth_sandbox_fallback)
    assert vt == "dev_verify_val"

    # 2. Environment is production, prod-* are strings
    auth_prod = {
        "environment": "production",
        "prod-id": "prod_id_val",
        "prod-secret": "prod_secret_val",
        "prod-verifier_token": "prod_verifier_token_val",
        "redirect_uri": "redirect_uri_val"
    }
    cid, sec, vt, cb = main._resolve_qbo_credentials(auth_prod)
    assert cid == "prod_id_val"
    assert sec == "prod_secret_val"
    assert vt == "prod_verifier_token_val"
    assert cb == "redirect_uri_val"

    # Test prod-verify fallback
    auth_prod_fallback = {
        "environment": "production",
        "prod-id": "prod_id_val",
        "prod-secret": "prod_secret_val",
        "prod-verify": "prod_verify_val",
        "redirect_uri": "redirect_uri_val"
    }
    _, _, vt, _ = main._resolve_qbo_credentials(auth_prod_fallback)
    assert vt == "prod_verify_val"


    # 3. Environment is sandbox, dev-* are NOT strings but client_id/client_secret/verifier_token fallback fields are strings
    auth_fallback_dict = {
        "environment": "sandbox",
        "client_id": "client_id_val",
        "client_secret": "client_secret_val",
        "verifier_token": "verifier_token_val"
    }
    cid, sec, vt, cb = main._resolve_qbo_credentials(auth_fallback_dict)
    assert cid == "client_id_val"
    assert sec == "client_secret_val"
    assert vt == "verifier_token_val"

    # 4. Environment is production, prod-* are NOT strings, fallback fields are NOT strings (env var fallback)
    auth_env_fallback = {
        "environment": "production"
    }
    with patch.dict(os.environ, {"QBO_CLIENT_ID": "env_id_val", "QBO_CLIENT_SECRET": "env_secret_val", "QBO_REDIRECT_URI": "env_redirect_val"}):
        cid, sec, vt, cb = main._resolve_qbo_credentials(auth_env_fallback)
        assert cid == "env_id_val"
        assert sec == "env_secret_val"
        assert vt is None
        assert cb == "env_redirect_val"



def test_process_booking_taken_slots_coverage():
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    local_tz = ZoneInfo("America/Los_Angeles")
    requested_dt = datetime.strptime("2026-06-15 10:00", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    
    # 1. Test valid ISO string matching requested slot
    transaction = MagicMock()
    inventory_ref = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.exists = True
    mock_snapshot.to_dict.return_value = {
        "taken_slots": [requested_dt.isoformat()]
    }
    inventory_ref.get.return_value = mock_snapshot
    with pytest.raises(ValueError, match="This time slot is already booked by another group."):
        main.process_booking_transaction(transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"})

    # 2. Test valid ISO string not matching requested slot, and invalid string causing ValueError
    mock_snapshot.to_dict.return_value = {
        "taken_slots": ["2026-06-15T11:00:00-07:00", "not-a-valid-date-string"]
    }
    # This should succeed since none matching
    booking_id = main.process_booking_transaction(transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"})
    assert booking_id is not None

    # 3. Test timezone-naive datetime object matching requested slot (it replaces/interprets as UTC)
    naive_dt_matching = requested_dt.astimezone(timezone.utc).replace(tzinfo=None)
    mock_snapshot.to_dict.return_value = {
        "taken_slots": [naive_dt_matching]
    }
    with pytest.raises(ValueError, match="This time slot is already booked by another group."):
        main.process_booking_transaction(transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"})

    # 4. Test timezone-aware datetime object matching requested slot
    mock_snapshot.to_dict.return_value = {
        "taken_slots": [requested_dt]
    }
    with pytest.raises(ValueError, match="This time slot is already booked by another group."):
        main.process_booking_transaction(transaction, inventory_ref, "2026-06-15", "10:00", 5, {"name": "Alice"})

