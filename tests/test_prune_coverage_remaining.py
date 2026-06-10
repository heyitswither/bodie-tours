import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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

def test_prune_unpaid_slots_errors():
    # Test cached DB return
    prune_unpaid_slots._cached_db = MagicMock(name='cached_db')
    assert prune_unpaid_slots._get_db() is prune_unpaid_slots._cached_db
    prune_unpaid_slots._cached_db = None

    # Force Dummy DB initialization branch logic
    orig_env = os.environ.get("FORCE_DUMMY_DB")
    os.environ["FORCE_DUMMY_DB"] = "1"
    try:
        # Re-initialize/trigger _get_db() warnings and exceptions
        import importlib
        importlib.reload(prune_unpaid_slots)
        assert prune_unpaid_slots._cached_db.__class__.__name__ == 'DummyFirestore'
    finally:
        if orig_env is None:
            del os.environ["FORCE_DUMMY_DB"]
        else:
            os.environ["FORCE_DUMMY_DB"] = orig_env
        import importlib
        importlib.reload(prune_unpaid_slots)

@patch('prune_unpaid_slots._get_m365_token_for_prune')
def test_prune_unpaid_slots_unexpected_error(mock_get_token, mock_db):
    mock_get_token.side_effect = Exception("Unexpected M365 error")
    # Simulate DB collection raising an exception to trigger outer try-except error handling
    mock_db.collection.side_effect = Exception("Database is down")
    
    builder = EnvironBuilder(method='POST')
    request = Request(builder.get_environ())
    response, status = prune_unpaid_slots.prune_unpaid_slots(request)
    assert status == 500
    assert response["status"] == "error"
    assert "Database is down" in response["message"]

@patch('prune_unpaid_slots._get_m365_token_for_prune')
def test_prune_unpaid_slots_remove_event_exception(mock_get_token, mock_db):
    mock_get_token.return_value = ("token", "ranger_1")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(hours=50)
    tour_datetime = created_at + timedelta(days=10)
    
    mock_doc = MagicMock()
    mock_doc.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime.isoformat(),
        "party_size": 2,
        "integration_ids": {"m365_event_id": "event_err"}
    }
    mock_db.collection.return_value.where.return_value.stream.return_value = [mock_doc]
    
    with patch('prune_unpaid_slots.process_cancellation_transaction', return_value=True), \
         patch('prune_unpaid_slots.remove_m365_event', side_effect=Exception("API limit exceeded")), \
         patch('prune_unpaid_slots.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        
        builder = EnvironBuilder(method='POST')
        request = Request(builder.get_environ())
        response, status = prune_unpaid_slots.prune_unpaid_slots(request)
        assert status == 200
        assert response["cancelled_count"] == 1

def test_send_outlook_reminder_custom_template_branches(mock_db):
    # Test custom template loading from Firestore
    mock_tmpl_doc = MagicMock()
    mock_tmpl_doc.to_dict.return_value = {
        "subject": "Custom subject {{booking_id}} with party {{party_size}}",
        "body": "Custom body {customer_name} {tour_datetime_str} link {payment_link} total {total_amount}"
    }
    mock_db.collection.return_value.document.return_value.get.return_value = mock_tmpl_doc
    
    # 1. Custom template selection via environment variable & placeholder assertions
    with patch.dict(os.environ, {"EMAIL_TEMPLATE_TYPE": "custom", "TOUR_PRICE_PER_PERSON": "30.00"}), \
         patch('requests.post') as mock_post:
        mock_post.return_value.status_code = 202
        res = prune_unpaid_slots.send_outlook_reminder(
            "token", "user", "cust@example.com", "Name", "2026-06-15 10:00", "id_123",
            payment_link="http://pay.me", party_size=3
        )
        assert res is True
        # Check that post was called with correct json payload
        args, kwargs = mock_post.call_args
        json_payload = kwargs['json']
        assert json_payload['message']['subject'] == "Custom subject id_123 with party 3"
        assert json_payload['message']['body']['content'] == "Custom body Name 2026-06-15 10:00 link http://pay.me total 90.00"
        
    # 2. Custom template Firestore fetch exception fallback to simple template
    mock_db.collection.return_value.document.return_value.get.side_effect = Exception("Firestore read error")
    with patch.dict(os.environ, {"EMAIL_TEMPLATE_TYPE": "custom"}), \
         patch('requests.post') as mock_post:
        mock_post.return_value.status_code = 202
        res = prune_unpaid_slots.send_outlook_reminder("token", "user", "cust@example.com", "Name", "2026-06-15 10:00", "id_123")
        assert res is True


@patch('requests.delete')
def test_remove_m365_event_calendar_id_branches(mock_delete, mock_db):
    # Set class name of db to be different from 'DummyFirestore'
    with patch.object(prune_unpaid_slots.db, '__class__') as mock_class:
        mock_class.__name__ = 'FirestoreClient'
        
        # 1. Document exists and has calendar_id
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"calendar_id": "cal_123"}
        mock_db.collection.return_value.document.return_value.get.return_value = mock_doc
        
        mock_delete.return_value.status_code = 204
        res = prune_unpaid_slots.remove_m365_event("token", "user", "event_123")
        assert res is True
        mock_delete.assert_called_with(
            "https://graph.microsoft.com/v1.0/users/user/calendars/cal_123/events/event_123",
            headers={"Authorization": "Bearer token"},
            timeout=10
        )
        
        # 2. Document does not exist (covers lines fallback)
        mock_doc.exists = False
        res = prune_unpaid_slots.remove_m365_event("token", "user", "event_123")
        assert res is True
        
        # 3. Document get raises exception (covers exception block)
        mock_db.collection.return_value.document.return_value.get.side_effect = Exception("Db failure")
        res = prune_unpaid_slots.remove_m365_event("token", "user", "event_123")
        assert res is True


def test_process_cancellation_transaction_value_error():
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()
    
    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {"payment_status": "PENDING"}
    mock_booking_ref.get.return_value = mock_booking_snap
    
    # Passing an invalid time_str (e.g. "invalid") causes ValueError, setting slot_dt_utc = None
    mock_inv_snap = MagicMock()
    mock_inv_snap.exists = True
    mock_inv_snap.to_dict.return_value = {
        "slots": {"invalid": {"taken": 5, "status": "AVAILABLE"}},
        "taken_slots": []
    }
    mock_inventory_ref.get.return_value = mock_inv_snap
    
    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "invalid"
    )
    assert res is True


def test_process_cancellation_transaction_invalid_datetime_taken_slots():
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.id = "2026-06-15"
    
    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {"payment_status": "PENDING"}
    mock_booking_ref.get.return_value = mock_booking_snap
    
    # taken_slots contains a string "10:00" that fails fromisoformat parsing but matches time_str in the fallback, covering line 288!
    mock_inv_snap = MagicMock()
    mock_inv_snap.exists = True
    mock_inv_snap.to_dict.return_value = {
        "slots": {"10:00": {"taken": 5, "status": "AVAILABLE"}},
        "taken_slots": ["10:00"]
    }
    mock_inventory_ref.get.return_value = mock_inv_snap
    
    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "10:00"
    )
    assert res is True


def test_prune_completed_tours_exceptions(mock_db):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    
    # 1. doc has no delete method, but has reference without path (covers fallback exception)
    mock_doc1 = MagicMock()
    mock_doc1.to_dict.return_value = {
        "payment_status": "PAID",
        "tour_datetime": datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc)
    }
    mock_doc1.reference = MagicMock(spec=[]) # No 'delete' and no 'path' attributes
    
    # 2. doc has delete method but it raises an exception (covers fallback exception block)
    mock_doc2 = MagicMock()
    mock_doc2.to_dict.return_value = {
        "payment_status": "CANCELLED_UNPAID",
        "tour_datetime": datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc)
    }
    mock_doc2.reference.delete.side_effect = Exception("Delete failure")
    
    mock_db.collection.return_value.where.return_value.stream.side_effect = [
        [mock_doc1],
        [mock_doc2]
    ]
    
    pruned = prune_unpaid_slots.prune_completed_tours(now)
    assert pruned == 1


def test_process_cancellation_transaction_exception_in_str():
    mock_transaction = MagicMock()
    mock_booking_ref = MagicMock()
    mock_inventory_ref = MagicMock()
    mock_inventory_ref.id = "2026-06-15"
    
    mock_booking_snap = MagicMock()
    mock_booking_snap.exists = True
    mock_booking_snap.to_dict.return_value = {"payment_status": "PENDING"}
    mock_booking_ref.get.return_value = mock_booking_snap
    
    class ErrorStr:
        def __str__(self):
            raise Exception("Error in string conversion")
            
    mock_inv_snap = MagicMock()
    mock_inv_snap.exists = True
    mock_inv_snap.to_dict.return_value = {
        "slots": {"10:00": {"taken": 5, "status": "AVAILABLE"}},
        "taken_slots": [ErrorStr()]
    }
    mock_inventory_ref.get.return_value = mock_inv_snap
    
    res = prune_unpaid_slots.process_cancellation_transaction(
        mock_transaction, mock_booking_ref, mock_inventory_ref, 2, "10:00"
    )
    assert res is True


def test_prune_completed_tours_invalid_path(mock_db):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    
    mock_doc = MagicMock()
    mock_doc.to_dict.return_value = {
        "payment_status": "PAID",
        "tour_datetime": datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc)
    }
    # Has path, but path is invalid and raises IndexError during split, triggering exception logging on line 351-353
    mock_ref = MagicMock()
    mock_ref.path = "invalid_path_no_slash"
    del mock_ref.delete # Ensure it goes to the fallback branch
    mock_doc.reference = mock_ref
    
    mock_db.collection.return_value.where.return_value.stream.side_effect = [
        [mock_doc],
        []
    ]
    
    pruned = prune_unpaid_slots.prune_completed_tours(now)
    assert pruned == 1


@patch('prune_unpaid_slots._get_m365_token_for_prune')
@patch('prune_unpaid_slots.send_outlook_reminder')
def test_prune_unpaid_slots_second_reminder(mock_send, mock_get_token, mock_db):
    mock_get_token.return_value = ("token", "ranger_1")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    
    # Lead time is 10 days (TTL = 48 hours), quarter_ttl_remaining is 36 hours.
    # We set created_at to 40 hours ago (booking_age = 40 hours).
    # Since reminder_sent is 1, it should send the second reminder and increment to 2.
    created_at = now - timedelta(hours=40)
    tour_datetime = created_at + timedelta(days=10)
    
    mock_doc = MagicMock()
    mock_doc.id = "doc_second_reminder"
    mock_doc.to_dict.return_value = {
        "created_at": created_at,
        "tour_datetime": tour_datetime,
        "reminder_sent": 1,
        "guest": {"name": "Alice", "email": "alice@example.com"}
    }
    
    mock_db.collection.return_value.where.return_value.stream.return_value = [mock_doc]
    mock_send.return_value = True
    
    with patch('prune_unpaid_slots.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        
        builder = EnvironBuilder(method='POST')
        request = Request(builder.get_environ())
        response, status = prune_unpaid_slots.prune_unpaid_slots(request)
        
        assert status == 200
        assert response["reminders_sent"] == 1
        mock_doc.reference.update.assert_called_once()


