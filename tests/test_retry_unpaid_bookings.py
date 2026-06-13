# tests/test_retry_unpaid_bookings.py
"""Unit tests for the retry_unpaid_bookings Cloud Function.
Ensures coverage by mocking external dependencies.
"""

import sys, os
import pytest
from unittest import mock

# Ensure project root is on the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from retry_unpaid_bookings import retry_unpaid_bookings, firestore


# Helper classes for mocking Firestore documents
class MockReference:
    def __init__(self):
        self.updates = []

    def update(self, payload):
        self.updates.append(payload)


class MockDoc:
    def __init__(self, data, ref):
        self.id = data.get("id", "doc1")
        self._data = data
        self.reference = ref

    def to_dict(self):
        return self._data


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    # Mock datetime.now to a fixed UTC datetime
    import datetime as dt_mod

    class FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt_mod.datetime(
                2026, 6, 9, 12, 0, 0, tzinfo=tz or dt_mod.timezone.utc
            )

    monkeypatch.setattr(dt_mod, "datetime", FixedDatetime)
    # Set environment variable for max retry attempts
    monkeypatch.setenv("MAX_RETRY_ATTEMPTS", "5")


def make_mock_firestore(docs):
    mock_collection = mock.MagicMock()
    mock_collection.where.return_value = mock_collection
    mock_collection.stream.return_value = docs
    return mock_collection


def test_successful_retry_and_email(monkeypatch):
    # Prepare mock document with retry_attempts < 3 and no prior email sent
    data = {
        "retry_attempts": 1,
        "email_sent_count": 0,
        "guest": {"email": "guest@example.com", "name": "Guest"},
        "date": "2026-07-01",
        "time": "10:00",
        "party_size": 2,
    }
    ref = MockReference()
    doc = MockDoc(data, ref)
    mock_collection = make_mock_firestore([doc])
    # Patch Firestore client used in the function
    monkeypatch.setattr(
        firestore,
        "Client",
        lambda **kwargs: mock.MagicMock(collection=lambda name: mock_collection),
    )
    # Mock external helpers
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_qbo_access_token", lambda: ("qbo_token", "realm")
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings.create_qbo_invoice",
        lambda token, realm, size, guest, **kwargs: ("inv123", "https://pay.example.com"),
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_m365_access_token", lambda: ("m365_token", "user_id")
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings._send_temp_issue_email", lambda *args, **kwargs: True
    )
    result, status = retry_unpaid_bookings(None)
    assert status == 200
    assert result["processed"] == 1
    assert result["retries"] == 1
    assert result["emails_sent"] == 1
    # Verify that the Firestore document was updated with retry and email flags
    update_payloads = ref.updates
    assert any(
        "retry_attempts" in upd and upd["retry_attempts"] == 2
        for upd in update_payloads
    )
    assert any("email_sent" in upd for upd in update_payloads)


def test_skip_due_to_max_retries(monkeypatch):
    data = {"retry_attempts": 3, "guest": {}, "date": None, "time": None}
    ref = MockReference()
    doc = MockDoc(data, ref)
    mock_collection = make_mock_firestore([doc])
    monkeypatch.setattr(
        firestore,
        "Client",
        lambda **kwargs: mock.MagicMock(collection=lambda name: mock_collection),
    )
    # No external calls should be invoked; ensure they raise if called
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_qbo_access_token",
        lambda: (_ for _ in ()).throw(RuntimeError()),
    )
    result, status = retry_unpaid_bookings(None)
    assert status == 200
    assert result["processed"] == 0
    assert result["retries"] == 0
    assert result["emails_sent"] == 0
    assert ref.updates == []


def test_skip_due_to_existing_invoice_id(monkeypatch):
    data = {
        "retry_attempts": 1,
        "guest": {"email": "guest@example.com", "name": "Guest"},
        "date": "2026-07-01",
        "time": "10:00",
        "party_size": 2,
        "integration_ids": {"qbo_invoice_id": "existing_invoice_123"},
    }
    ref = MockReference()
    doc = MockDoc(data, ref)
    mock_collection = make_mock_firestore([doc])
    monkeypatch.setattr(
        firestore,
        "Client",
        lambda **kwargs: mock.MagicMock(collection=lambda name: mock_collection),
    )
    # Ensure no external calls are invoked; they should raise RuntimeError if called
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_qbo_access_token",
        lambda: (_ for _ in ()).throw(RuntimeError("Should not be called")),
    )
    result, status = retry_unpaid_bookings(None)
    assert status == 200
    assert result["processed"] == 0
    assert result["retries"] == 0
    assert result["emails_sent"] == 0
    assert ref.updates == []
