"""
Shared conftest helpers for all e2e test tiers.
Provides the mock_main_db autouse fixture and URL-smart mock_requests_post.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta


def _make_mock_db():
    """Build a mock db that provides valid tokens and passes the booking transaction."""
    far_future = datetime.now(timezone.utc) + timedelta(hours=2)

    base_config = {
        "user_id": "ranger@bodie.gov",
        "client_id": "mock_client",
        "client_secret": "mock_secret",
        "tenant_id": "mock_tenant",
        "refresh_token": "mock_refresh",
        "access_token": "mock_valid_access_token",
        "expires_at": far_future,
        "realmId": "mock_realm",
    }

    mock_db = MagicMock()

    def _collection(name):
        coll = MagicMock()

        def _doc(doc_name=None):
            doc = MagicMock()
            doc.id = "mock_booking_id_123"
            if name == "config":
                get_result = MagicMock()
                get_result.exists = True
                get_result.to_dict.return_value = base_config
                doc.get.return_value = get_result
                doc.update.return_value = None
            else:
                # Inventory / booking documents — empty by default
                get_result = MagicMock()
                get_result.exists = False
                get_result.to_dict.return_value = {}
                doc.get.return_value = get_result
            return doc

        coll.document = _doc
        coll.where.return_value.stream.return_value = iter([])
        return coll

    mock_db.collection.side_effect = _collection
    mock_db.transaction.return_value = MagicMock()
    return mock_db


@pytest.fixture(autouse=True)
def mock_main_db():
    """Patch main.db and prune_unpaid_slots.db with valid token responses."""
    import main
    import prune_unpaid_slots

    mock_db = _make_mock_db()
    with patch("main.db", mock_db), patch("prune_unpaid_slots.db", mock_db):
        yield mock_db


def _url_based_post_response(url, *args, **kwargs):
    """Default requests.post side_effect that routes by URL pattern."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()

    url_lower = url.lower()
    if "microsoftonline" in url_lower or (
        "oauth" in url_lower and "intuit" not in url_lower
    ):
        resp.json.return_value = {
            "access_token": "mock_m365_token",
            "refresh_token": "mock_refresh",
            "expires_in": 3600,
        }
    elif "/events" in url_lower and "graph.microsoft" in url_lower:
        resp.status_code = 201
        resp.json.return_value = {"id": "mock_event_id_abc"}
    elif "invoice" in url_lower or "quickbooks" in url_lower or "intuit" in url_lower:
        if "tokens" in url_lower:
            resp.json.return_value = {
                "access_token": "mock_qbo_token",
                "refresh_token": "mock_qbo_refresh",
                "expires_in": 3600,
            }
        else:
            resp.json.return_value = {"Invoice": {"Id": "INV-MOCK-001"}}
    elif "sendmail" in url_lower:
        resp.status_code = 202
        resp.json.return_value = {}
    else:
        resp.json.return_value = {}

    return resp


def _url_based_get_response(url, *args, **kwargs):
    """Default requests.get side_effect for M365 calendarView availability checks.

    Returns a single 'Touring Hours' / Free event covering a 9 AM – 6 PM window
    so that any time slot within that range passes the whitelist check.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()

    if "calendarView" in url and "graph.microsoft" in url.lower():
        resp.json.return_value = {
            "value": [
                {
                    "subject": "Touring Hours",
                    "showAs": "free",
                    "start": {
                        "dateTime": "2026-01-01T09:00:00",
                        "timeZone": "Pacific Standard Time",
                    },
                    "end": {
                        "dateTime": "2026-12-31T18:00:00",
                        "timeZone": "Pacific Standard Time",
                    },
                }
            ]
        }
    else:
        resp.json.return_value = {}

    return resp


@pytest.fixture
def mock_requests_post():
    """URL-aware requests.post mock. Returns sensible defaults per endpoint."""
    with patch("requests.post") as mock_post:
        mock_post.side_effect = _url_based_post_response
        yield mock_post


@pytest.fixture(autouse=True)
def mock_requests_get():
    """URL-aware requests.get mock. Autouse so calendarView is always patched.
    Returns a Touring Hours / Free event covering any time slot by default."""
    with patch("requests.get") as mock_get:
        mock_get.side_effect = _url_based_get_response
        yield mock_get
