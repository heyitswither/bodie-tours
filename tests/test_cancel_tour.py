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
from flask import Request
from werkzeug.test import EnvironBuilder


@pytest.fixture
def mock_main_db():
    with patch("main.db") as mock_db:
        yield mock_db


def test_cancel_tour_options_preflight(mock_main_db):
    """CORS OPTIONS preflight check should return 204."""
    builder = EnvironBuilder(
        method="OPTIONS",
        headers={"Origin": "https://www.bodiefoundation.org"}
    )
    request = Request(builder.get_environ())
    body, status, headers = main.cancel_tour(request)
    assert status == 204
    assert headers.get("Access-Control-Allow-Origin") == "https://www.bodiefoundation.org"


def test_cancel_tour_missing_parameters(mock_main_db):
    """Request missing booking_id or token should return 400."""
    builder = EnvironBuilder(method="GET", query_string="")
    request = Request(builder.get_environ())
    body, status, headers = main.cancel_tour(request)
    assert status == 400
    assert body["status"] == "error"
    assert "Missing booking_id or token" in body["message"]


def test_cancel_tour_not_found(mock_main_db):
    """When the booking document does not exist, return 404."""
    builder = EnvironBuilder(method="GET", query_string="booking_id=nonexistent&token=some_token")
    request = Request(builder.get_environ())
    
    mock_doc = MagicMock()
    mock_doc.exists = False
    mock_main_db.collection.return_value.document.return_value.get.return_value = mock_doc
    
    body, status, headers = main.cancel_tour(request)
    assert status == 404
    assert body["status"] == "error"
    assert "Booking not found" in body["message"]


def test_cancel_tour_invalid_token(mock_main_db):
    """When the token does not match the booking token, return 403."""
    builder = EnvironBuilder(method="GET", query_string="booking_id=b123&token=wrong_token")
    request = Request(builder.get_environ())
    
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "token": "correct_token"
    }
    mock_main_db.collection.return_value.document.return_value.get.return_value = mock_doc
    
    body, status, headers = main.cancel_tour(request)
    assert status == 403
    assert body["status"] == "error"
    assert "Invalid token" in body["message"]


def test_cancel_tour_success(mock_main_db):
    """When booking exists and token is valid, cancel reservation, release slots and return 200."""
    builder = EnvironBuilder(method="GET", query_string="booking_id=b123&token=correct_token")
    request = Request(builder.get_environ())
    
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "token": "correct_token",
        "tour_datetime": "2026-06-15T10:00:00Z",
        "duration_hours": 2
    }
    
    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = mock_doc
    mock_main_db.collection.return_value.document.return_value = mock_doc_ref
    
    # We mock ArrayRemove since we call it
    with patch("main.firestore.ArrayRemove") as mock_array_remove:
        body, status, headers = main.cancel_tour(request)
        
        # Verify status is 200
        assert status == 200
        assert body["status"] == "success"
        assert body["message"] == "Booking cancelled"
        
        # Verify the booking is updated to CANCELLED_BY_GUEST
        mock_doc_ref.update.assert_any_call({"payment_status": "CANCELLED_BY_GUEST"})


def test_cancel_tour_unexpected_exception(mock_main_db):
    """When an unexpected database exception occurs, return 500."""
    builder = EnvironBuilder(method="GET", query_string="booking_id=b123&token=some_token")
    request = Request(builder.get_environ())
    
    mock_main_db.collection.side_effect = Exception("Database is down")
    
    body, status, headers = main.cancel_tour(request)
    assert status == 500
    assert body["status"] == "error"
    assert "Database is down" in body["message"]
