import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Ensure google.cloud.firestore is mocked before import
if 'google.cloud.firestore' not in sys.modules:
    mock_firestore = MagicMock()
    mock_firestore.transactional = lambda f: f
    sys.modules['google.cloud.firestore'] = mock_firestore

import prune_unpaid_slots
from datetime import datetime, timezone, timedelta
from flask import Request
from werkzeug.test import EnvironBuilder

@pytest.fixture
def mock_db():
    with patch('prune_unpaid_slots.db') as mock_db:
        mock_query = MagicMock()
        mock_db.collection.return_value.where.return_value = mock_query
        mock_query.where.return_value = mock_query
        yield mock_db

# ===========================================================================
# 1. _get_m365_token_for_prune Tests (Gaps 1, 2, 3, 4)
# ===========================================================================

def test_get_m365_token_missing_auth(mock_db):
    # Gap 1: raise Exception("M365 Auth configuration missing.")
    mock_doc = MagicMock()
    mock_doc.get.return_value.to_dict.return_value = None
    mock_db.collection.return_value.document.return_value = mock_doc

    with pytest.raises(Exception, match="M365 Auth configuration missing."):
        prune_unpaid_slots._get_m365_token_for_prune()

def test_get_m365_token_valid_cached(mock_db):
    # True path of line 28: returns cached token
    mock_doc = MagicMock()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger_123",
        "access_token": "cached_access_token",
        "expires_at": expires_at
    }
    mock_db.collection.return_value.document.return_value = mock_doc

    token, user_id = prune_unpaid_slots._get_m365_token_for_prune()
    assert token == "cached_access_token"
    assert user_id == "ranger_123"

@patch('requests.post')
def test_get_m365_token_expired_http_failure(mock_post, mock_db):
    # Gap 3: raise Exception(f"Failed to refresh M365 token: {response.text}")
    # Gap 2 (branch 28->31): expires_at is in the past, token is refreshed
    mock_doc = MagicMock()
    expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger_123",
        "access_token": "cached_access_token",
        "expires_at": expires_at,
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_post.return_value = mock_resp

    with pytest.raises(Exception, match="Failed to refresh M365 token: Internal Server Error"):
        prune_unpaid_slots._get_m365_token_for_prune()

@patch('requests.post')
def test_get_m365_token_refresh_same_refresh_token(mock_post, mock_db):
    # Gap 4 (branch 54->56): new refresh token is absent or same, not updated
    mock_doc = MagicMock()
    expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger_123",
        "access_token": "cached_access_token",
        "expires_at": expires_at,
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new_access_token",
        "expires_in": 3600
    }
    mock_post.return_value = mock_resp

    token, user_id = prune_unpaid_slots._get_m365_token_for_prune()
    assert token == "new_access_token"
    mock_doc.update.assert_called_once()
    # verify refresh_token was NOT updated
    update_data = mock_doc.update.call_args[0][0]
    assert "refresh_token" not in update_data

@patch('requests.post')
def test_get_m365_token_refresh_new_refresh_token(mock_post, mock_db):
    # Gap 4: new refresh token is present and different
    mock_doc = MagicMock()
    expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    mock_doc.get.return_value.to_dict.return_value = {
        "user_id": "ranger_123",
        "access_token": "cached_access_token",
        "expires_at": expires_at,
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "refresh_token": "old_rtoken"
    }
    mock_db.collection.return_value.document.return_value = mock_doc

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new_access_token",
        "refresh_token": "new_rtoken",
        "expires_in": "3600"
    }
    mock_post.return_value = mock_resp

    token, user_id = prune_unpaid_slots._get_m365_token_for_prune()
    assert token == "new_access_token"
    mock_doc.update.assert_called_once()
    update_data = mock_doc.update.call_args[0][0]
    assert update_data["refresh_token"] == "new_rtoken"

# ===========================================================================
# 2. Outlook Reminder and Event Removal Tests (Gaps 5, 6)
# ===========================================================================

@patch('requests.post')
def test_send_outlook_reminder_success(mock_post):
    # Gap 5: returns True on 200/202 status code
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_post.return_value = mock_resp

    res = prune_unpaid_slots.send_outlook_reminder(
        "token", "user", "cust@example.com", "Cust Name", "2026-06-15 10:00", "booking_12"
    )
    assert res is True

@patch('requests.post')
def test_send_outlook_reminder_failure(mock_post):
    # Gap 5: returns False on other status codes
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_post.return_value = mock_resp

    res = prune_unpaid_slots.send_outlook_reminder(
        "token", "user", "cust@example.com", "Cust Name", "2026-06-15 10:00", "booking_12"
    )
    assert res is False

@patch('requests.delete')
def test_remove_m365_event_success(mock_delete):
    # Gap 6: returns True on 204
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_delete.return_value = mock_resp

    res = prune_unpaid_slots.remove_m365_event("token", "user", "event_123")
    assert res is True

@patch('requests.delete')
def test_remove_m365_event_failure(mock_delete):
    # Gap 6: returns False on not 204
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_delete.return_value = mock_resp

    res = prune_unpaid_slots.remove_m365_event("token", "user", "event_123")
    assert res is False

# ===========================================================================
# 3. Dynamic TTL Calculation Tests (Gap 7)
# ===========================================================================

def test_calculate_ttl_cases():
    # Gap 7: cover all boundary cases of calculate_ttl
    # Case 1: Lead time >= 7 days -> 48 hours
    created_at = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    tour_dt = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)
    assert prune_unpaid_slots.calculate_ttl(created_at, tour_dt) == timedelta(hours=48)

    # Case 2: Lead time >= 2 days (but < 7 days) -> 24 hours
    tour_dt = datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
    assert prune_unpaid_slots.calculate_ttl(created_at, tour_dt) == timedelta(hours=24)

    # Case 3: Lead time >= 1 day (but < 2 days) -> 3 hours
    tour_dt = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    assert prune_unpaid_slots.calculate_ttl(created_at, tour_dt) == timedelta(hours=3)

    # Case 4: Lead time < 1 day -> 1 hour
    tour_dt = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    assert prune_unpaid_slots.calculate_ttl(created_at, tour_dt) == timedelta(hours=1)

# ===========================================================================
# 4. process_cancellation_transaction Tests (Gaps 8, 9, 10)
# ===========================================================================

def test_process_cancellation_booking_not_exists():
    # Gap 8: booking document does not exist
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()

    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = False
    mock_booking_ref.get.return_value = mock_booking_snap

    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "10:00"
    )
    assert res is False

def test_process_cancellation_booking_not_pending():
    # Gap 9: booking payment_status is not PENDING
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()

    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {"payment_status": "PAID"}
    mock_booking_ref.get.return_value = mock_booking_snap

    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "10:00"
    )
    assert res is False

def test_process_cancellation_inventory_not_exists():
    # Gap 10: inventory snapshot does not exist
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()

    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {"payment_status": "PENDING"}
    mock_booking_ref.get.return_value = mock_booking_snap

    mock_inv_snap = MagicMock()
    mock_inv_snap.exists = False
    mock_inventory_ref.get.return_value = mock_inv_snap

    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "10:00"
    )
    assert res is True
    # Verify booking status updated, but transaction.set for inventory was not called
    mock_transaction.update.assert_called_once_with(mock_booking_ref, {"payment_status": "CANCELLED_UNPAID"})
    mock_transaction.set.assert_not_called()

# ===========================================================================
# 5. Completed Tour Pruning Tests (Gaps 11, 12, 13)
# ===========================================================================

def test_prune_completed_tours_cases(mock_db):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # Document 1: missing tour_datetime (Gap 11)
    mock_doc1 = MagicMock()
    mock_doc1.to_dict.return_value = {"payment_status": "PAID"}

    # Document 2: ValueError when parsing (Gap 12)
    mock_doc2 = MagicMock()
    mock_doc2.to_dict.return_value = {"payment_status": "PAID", "tour_datetime": "invalid-iso"}

    # Document 3: tour_datetime is naive (no tzinfo) (Gap 12)
    mock_doc3 = MagicMock()
    mock_doc3.to_dict.return_value = {
        "payment_status": "PAID",
        "tour_datetime": "2026-06-14T10:00:00"
    }

    # Document 4: tour_datetime is on another day (not yesterday)
    mock_doc4 = MagicMock()
    mock_doc4.to_dict.return_value = {
        "payment_status": "PAID",
        "tour_datetime": "2026-06-13T10:00:00Z"
    }

    # Document 5: yesterday and succeeds (Gap 13)
    mock_doc5 = MagicMock()
    mock_doc5.to_dict.return_value = {
        "payment_status": "CANCELLED_UNPAID",
        "tour_datetime": datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc)
    }


    # Mock collection streams
    # First stream for "PAID" returns mock_doc3 (yesterday naive)
    # Second stream for "CANCELLED_UNPAID" returns mock_doc5 (yesterday with Z timezone)
    mock_db.collection.return_value.where.return_value.stream.side_effect = [
        [mock_doc3],
        [mock_doc5]
    ]

    pruned = prune_unpaid_slots.prune_completed_tours(now)
    # mock_doc3 should be pruned (yesterday naive)
    # mock_doc5 should be pruned (yesterday with Z timezone)
    assert pruned == 2
    mock_doc3.reference.delete.assert_called_once()
    mock_doc5.reference.delete.assert_called_once()
    mock_doc1.reference.delete.assert_not_called()
    mock_doc2.reference.delete.assert_not_called()
    mock_doc4.reference.delete.assert_not_called()

# ===========================================================================
# 6. prune_unpaid_slots Main HTTP Entry Tests (Gaps 14, 15, 16, 17, 18)
# ===========================================================================

@patch('prune_unpaid_slots._get_m365_token_for_prune')
def test_prune_unpaid_slots_m365_unavailable(mock_get_token, mock_db):
    # Exception inside _get_m365_token_for_prune -> m365_available = False
    mock_get_token.side_effect = Exception("M365 Token Down")
    
    mock_db.collection.return_value.where.return_value.stream.return_value = []
    
    builder = EnvironBuilder(method='POST')
    request = Request(builder.get_environ())
    response, status = prune_unpaid_slots.prune_unpaid_slots(request)
    assert status == 200
    assert response["cancelled_count"] == 0

def test_prune_unpaid_slots_booking_validation_skips(mock_db):
    # Gap 14: Booking doc with missing tour_datetime or created_at
    mock_doc1 = MagicMock()
    mock_doc1.to_dict.return_value = {"created_at": datetime.now(timezone.utc)}

    # Gap 15: ValueError in tour_datetime parsing
    mock_doc2 = MagicMock()
    mock_doc2.to_dict.return_value = {
        "created_at": datetime.now(timezone.utc),
        "tour_datetime": "invalid-format"
    }

    # Gap 16: created_at without tzinfo attribute
    mock_doc3 = MagicMock()
    # we use an object that lacks timezone/attribute properties or just doesn't look like datetime
    mock_doc3.to_dict.return_value = {
        "created_at": "not-a-datetime",
        "tour_datetime": "2026-06-15T10:00:00Z"
    }

    # Gap 16: created_at without timezone (naive datetime)
    mock_doc4 = MagicMock()
    mock_doc4.to_dict.return_value = {
        "created_at": datetime(2026, 6, 1, 10, 0), # naive
        "tour_datetime": "2026-06-15T10:00:00Z"
    }

    # Gap 15: naive tour_datetime (without Z) to cover line 250
    mock_doc5 = MagicMock()
    mock_doc5.to_dict.return_value = {
        "created_at": "not-a-datetime",
        "tour_datetime": "2026-06-15T10:00:00"
    }

    # Set up mocks to skip these and run successfully
    mock_db.collection.return_value.where.return_value.stream.return_value = [
        mock_doc1, mock_doc2, mock_doc3, mock_doc4, mock_doc5
    ]

    builder = EnvironBuilder(method='POST')
    request = Request(builder.get_environ())
    response, status = prune_unpaid_slots.prune_unpaid_slots(request)
    assert status == 200
    assert response["cancelled_count"] == 0

@patch('prune_unpaid_slots._get_m365_token_for_prune')
@patch('prune_unpaid_slots.send_outlook_reminder')
def test_prune_unpaid_slots_reminders(mock_send, mock_get_token, mock_db):
    mock_get_token.return_value = ("token", "ranger_1")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # Lead time is 10 days (TTL = 48 hours), created 30 hours ago (age = 30h)
    # This falls in the range [24h, 48h] which is [half_ttl, ttl] -> should send reminder
    created_at = now - timedelta(hours=30)
    tour_datetime = created_at + timedelta(days=10)

    # Document 1: reminder sent successfully
    mock_doc1 = MagicMock()
    mock_doc1.id = "doc1"
    mock_doc1.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime,
        "guest": {"name": "Alice", "email": "alice@example.com"}
    }


    # Document 2: reminder send fails
    mock_doc2 = MagicMock()
    mock_doc2.id = "doc2"
    mock_doc2.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime.isoformat(),
        "customer": {"name": "Bob", "email": "bob@example.com"}
    }

    # Document 3: reminder update reference raises exception (Gap 17)
    mock_doc3 = MagicMock()
    mock_doc3.id = "doc3"
    mock_doc3.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime.isoformat(),
        "guest": {"email": "charlie@example.com"} # no name
    }
    mock_doc3.reference.update.side_effect = Exception("Db failure")

    mock_db.collection.return_value.where.return_value.stream.return_value = [
        mock_doc1, mock_doc2, mock_doc3
    ]

    # Return True, False, True for the three reminder attempts
    mock_send.side_effect = [True, False, True]

    with patch('prune_unpaid_slots.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat

        builder = EnvironBuilder(method='POST')
        request = Request(builder.get_environ())
        response, status = prune_unpaid_slots.prune_unpaid_slots(request)

        assert status == 200
        # doc1 and doc3 reminders were sent
        assert response["reminders_sent"] == 2
        mock_doc1.reference.update.assert_called_once_with({"reminder_sent": True})
        mock_doc3.reference.update.assert_called_once_with({"reminder_sent": True})

@patch('prune_unpaid_slots._get_m365_token_for_prune')
@patch('prune_unpaid_slots.process_cancellation_transaction')
@patch('prune_unpaid_slots.remove_m365_event')
def test_prune_unpaid_slots_cancellation_cases(mock_remove_event, mock_process, mock_get_token, mock_db):
    mock_get_token.return_value = ("token", "ranger_1")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # Lead time is 10 days (TTL = 48 hours), created 50 hours ago (age = 50h > TTL)
    created_at = now - timedelta(hours=50)
    tour_datetime = created_at + timedelta(days=10)

    # Document 1: Cancellation fails (Gap 18)
    mock_doc1 = MagicMock()
    mock_doc1.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime.isoformat(),
        "party_size": 3
    }

    # Document 2: Cancellation succeeds, with event_id
    mock_doc2 = MagicMock()
    mock_doc2.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime.isoformat(),
        "party_size": 2,
        "integration_ids": {"m365_event_id": "event_999"}
    }

    mock_db.collection.return_value.where.return_value.stream.return_value = [
        mock_doc1, mock_doc2
    ]

    mock_process.side_effect = [False, True]

    with patch('prune_unpaid_slots.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat

        builder = EnvironBuilder(method='POST')
        request = Request(builder.get_environ())
        response, status = prune_unpaid_slots.prune_unpaid_slots(request)

        assert status == 200
        assert response["cancelled_count"] == 1
        mock_remove_event.assert_called_once_with("token", "ranger_1", "event_999")

# ===========================================================================
# 7. process_cancellation_transaction Taken Slots Coverage
# ===========================================================================

def test_process_cancellation_taken_slots_coverage():
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    local_tz = ZoneInfo("America/Los_Angeles")
    
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.id = "2029-06-03"

    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {"payment_status": "PENDING"}
    mock_booking_ref.get.return_value = mock_booking_snap

    # target slot to cancel is "18:00"
    slot_dt = datetime.strptime("2029-06-03 18:00", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    slot_dt_utc = slot_dt.astimezone(timezone.utc)
    
    # Populate taken_slots with various types:
    ts_matching_str = slot_dt.isoformat()
    ts_nonmatching_str = "2029-06-03T19:00:00-07:00"
    ts_invalid_str = "not-a-datetime"
    ts_matching_dt_tz = slot_dt
    ts_nonmatching_dt_tz = datetime.strptime("2029-06-03 19:00", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    ts_matching_dt_naive = slot_dt_utc.replace(tzinfo=None)
    ts_nonmatching_dt_naive = datetime.strptime("2029-06-03 19:00", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)
    
    taken_slots = [
        ts_matching_str,
        ts_nonmatching_str,
        ts_invalid_str,
        ts_matching_dt_tz,
        ts_nonmatching_dt_tz,
        ts_matching_dt_naive,
        ts_nonmatching_dt_naive
    ]
    
    mock_inv_snap = MagicMock()
    mock_inv_snap.exists = True
    mock_inv_snap.to_dict.return_value = {
        "slots": {"18:00": {"taken": 4, "status": "SOLD_OUT"}},
        "taken_slots": taken_slots
    }
    mock_inventory_ref.get.return_value = mock_inv_snap

    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 4, "18:00"
    )
    assert res is True

    # Check that transaction.set was called to update inventory, and inspect the new_taken_slots:
    mock_transaction.set.assert_called_once()
    set_args = mock_transaction.set.call_args[0][1]
    new_taken_slots = set_args.get("taken_slots")
    
    assert ts_nonmatching_str in new_taken_slots
    assert ts_nonmatching_dt_tz in new_taken_slots
    assert ts_nonmatching_dt_naive in new_taken_slots
    
    assert ts_matching_str not in new_taken_slots
    assert ts_invalid_str not in new_taken_slots
    assert ts_matching_dt_tz not in new_taken_slots
    assert ts_matching_dt_naive not in new_taken_slots


