import pytest
from unittest.mock import patch, MagicMock
from flask import Request
from werkzeug.test import EnvironBuilder
from datetime import datetime, timezone, timedelta
import sys
import os

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Mock firestore before importing prune_unpaid_slots
if 'google.cloud.firestore' in sys.modules:
    mock_firestore = sys.modules['google.cloud.firestore']
else:
    mock_firestore = MagicMock()
    mock_firestore.transactional = lambda f: f
    sys.modules['google.cloud.firestore'] = mock_firestore
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # sys.modules['google.cloud'] = MagicMock(firestore=mock_firestore)

import prune_unpaid_slots

@pytest.fixture
def mock_db():
    with patch('prune_unpaid_slots.db') as mock_db:
        mock_query = MagicMock()
        mock_db.collection.return_value.where.return_value = mock_query
        mock_query.where.return_value = mock_query
        yield mock_db

def test_prune_unpaid_slots_no_bookings(mock_db):
    # Mock no pending bookings
    mock_db.collection.return_value.where.return_value.stream.return_value = []
    
    builder = EnvironBuilder(method='POST')
    env = builder.get_environ()
    request = Request(env)
    
    response, status_code = prune_unpaid_slots.prune_unpaid_slots(request)
    
    assert status_code == 200
    assert response['status'] == 'success'
    assert response['cancelled_count'] == 0

def test_prune_unpaid_slots_expired(mock_db):
    now = datetime.now(timezone.utc)
    
    # Created 8 days ago, lead time is 10 days, ttl is 48 hours
    created_at = now - timedelta(days=8)
    tour_datetime = created_at + timedelta(days=10)
    # The booking is expired because now - created_at (8 days) > TTL (48 hours)
    
    mock_booking_doc = MagicMock()
    mock_booking_doc.to_dict.return_value = {
        "tour_datetime": tour_datetime.isoformat(),
        "created_at": created_at,
        "party_size": 2,
        "payment_status": "PENDING"
    }
    
    mock_db.collection.return_value.where.return_value.stream.return_value = [mock_booking_doc]
    
    # Mock transaction success
    with patch('prune_unpaid_slots.process_cancellation_transaction', return_value=True) as mock_process:
        builder = EnvironBuilder(method='POST')
        env = builder.get_environ()
        request = Request(env)
        
        response, status_code = prune_unpaid_slots.prune_unpaid_slots(request)
        
        assert status_code == 200
        assert response['cancelled_count'] == 1
        mock_process.assert_called_once()

def test_prune_unpaid_slots_not_expired(mock_db):
    now = datetime.now(timezone.utc)
    
    # Created 1 hour ago, lead time is 10 days, ttl is 48 hours
    created_at = now - timedelta(hours=1)
    tour_datetime = created_at + timedelta(days=10)
    # The booking is not expired because now - created_at (1 hour) < TTL (48 hours)
    
    mock_booking_doc = MagicMock()
    mock_booking_doc.to_dict.return_value = {
        "tour_datetime": tour_datetime.isoformat(),
        "created_at": created_at,
        "party_size": 2,
        "payment_status": "PENDING"
    }
    
    mock_db.collection.return_value.where.return_value.stream.return_value = [mock_booking_doc]
    
    with patch('prune_unpaid_slots.process_cancellation_transaction', return_value=True) as mock_process:
        builder = EnvironBuilder(method='POST')
        env = builder.get_environ()
        request = Request(env)
        
        response, status_code = prune_unpaid_slots.prune_unpaid_slots(request)
        
        assert status_code == 200
        assert response['cancelled_count'] == 0
        mock_process.assert_not_called()

def test_process_cancellation_transaction():
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.id = "2026-06-15"
    
    mock_booking_snapshot = MagicMock()
    mock_booking_snapshot.exists = True
    mock_booking_snapshot.to_dict.return_value = {"payment_status": "PENDING"}
    mock_booking_ref.get.return_value = mock_booking_snapshot
    
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo("America/Los_Angeles")
    slot_dt = datetime(2026, 6, 15, 10, 0, tzinfo=local_tz)
    
    mock_inventory_snapshot = MagicMock()
    mock_inventory_snapshot.exists = True
    mock_inventory_snapshot.to_dict.return_value = {
        "slots": {
            "10:00": {"taken": 5, "status": "AVAILABLE"}
        },
        "taken_slots": [slot_dt]
    }
    mock_inventory_ref.get.return_value = mock_inventory_snapshot
    
    success = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "10:00"
    )
    
    assert success is True
    
    # Verify inventory was updated correctly
    mock_transaction.set.assert_called_once()
    args, kwargs = mock_transaction.set.call_args
    assert args[0] == mock_inventory_ref
    assert args[1]["taken_slots"] == []
    assert args[1]["slots"] == prune_unpaid_slots.firestore.DELETE_FIELD
    
    # Verify booking status was updated
    mock_transaction.update.assert_called_once_with(
        mock_booking_ref, {"payment_status": "CANCELLED_UNPAID"}
    )


def test_pruning_reminder_guest_and_duplicate_prevention(mock_db):
    now = datetime.now(timezone.utc)
    
    # Lead time is 10 days (TTL = 48 hours), created 30 hours ago (age = 30h)
    # This falls in the range [24h, 48h] which is [half_ttl, ttl] -> should send reminder
    created_at = now - timedelta(hours=30)
    tour_datetime = created_at + timedelta(days=10)
    
    # 1. Test schema mapping: "guest" field correctly parsed
    mock_booking_doc = MagicMock()
    mock_booking_doc.id = "booking_guest_abc"
    mock_booking_doc.to_dict.return_value = {
        "tour_datetime": tour_datetime.isoformat(),
        "created_at": created_at,
        "party_size": 2,
        "payment_status": "PENDING",
        "guest": {
            "name": "Alice Guest",
            "email": "alice@example.com"
        }
    }
    
    mock_db.collection.return_value.where.return_value.stream.return_value = [mock_booking_doc]
    
    # Mock M365 Auth configuration and send_outlook_reminder success
    mock_auth_doc = MagicMock()
    mock_auth_doc.to_dict.return_value = {
        "user_id": "ranger@bodie.gov",
        "access_token": "valid_token",
        "expires_at": now + timedelta(hours=2)
    }
    # Mock config document get
    mock_db.collection.return_value.document.return_value.get.return_value = mock_auth_doc
    
    prune_unpaid_slots.firestore.Increment.return_value = "increment_sentinel"
    with patch('prune_unpaid_slots.send_outlook_reminder', return_value=True) as mock_send_reminder:
        builder = EnvironBuilder(method='POST')
        request = Request(builder.get_environ())
        
        response, status_code = prune_unpaid_slots.prune_unpaid_slots(request)
        
        assert status_code == 200
        assert response['reminders_sent'] == 1
        from zoneinfo import ZoneInfo
        tour_datetime_str = tour_datetime.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")
        # Check that send_outlook_reminder was called with the guest's email and name
        mock_send_reminder.assert_called_once_with(
            "valid_token", "ranger@bodie.gov", "alice@example.com", "Alice Guest", tour_datetime_str, "booking_guest_abc",
            payment_link=None, party_size=2, token=None
        )
        # Check that reminder_sent: "increment_sentinel" was written to Firestore
        mock_booking_doc.reference.update.assert_called_once_with({"reminder_sent": "increment_sentinel"})

    # 2. Test duplicate prevention: when reminder_sent is 2, it should not send email again
    mock_booking_doc_sent = MagicMock()
    mock_booking_doc_sent.id = "booking_guest_abc"
    mock_booking_doc_sent.to_dict.return_value = {
        "tour_datetime": tour_datetime.isoformat(),
        "created_at": created_at,
        "party_size": 2,
        "payment_status": "PENDING",
        "guest": {
            "name": "Alice Guest",
            "email": "alice@example.com"
        },
        "reminder_sent": 2
    }
    
    mock_db.collection.return_value.where.return_value.stream.return_value = [mock_booking_doc_sent]
    
    with patch('prune_unpaid_slots.send_outlook_reminder', return_value=True) as mock_send_reminder:
        builder = EnvironBuilder(method='POST')
        request = Request(builder.get_environ())
        
        response, status_code = prune_unpaid_slots.prune_unpaid_slots(request)
        
        assert status_code == 200
        assert response['reminders_sent'] == 0
        mock_send_reminder.assert_not_called()
