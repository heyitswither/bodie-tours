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
        "payment_link": "https://pay.example.com",
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


def test_skip_past_ttl(monkeypatch):
    from retry_unpaid_bookings import datetime
    import datetime as dt_mod
    now_val = datetime.now(dt_mod.timezone.utc)

    # Lead time < 1 day -> TTL = 1 hour 15 mins.
    # If booking is older than 1 hour 15 mins, it should be skipped.
    tour_dt = now_val + dt_mod.timedelta(hours=10) # 10h lead time, TTL = 1h15m
    created_at = now_val - dt_mod.timedelta(hours=2) # 2 hours old, older than 1h15m

    data = {
        "retry_attempts": 1,
        "email_sent_count": 0,
        "guest": {"email": "guest@example.com", "name": "Guest"},
        "tour_datetime": tour_dt,
        "created_at": created_at,
        "party_size": 2,
    }
    ref = MockReference()
    doc = MockDoc(data, ref)
    mock_collection = make_mock_firestore([doc])
    monkeypatch.setattr(
        firestore,
        "Client",
        lambda **kwargs: mock.MagicMock(collection=lambda name: mock_collection),
    )
    # Ensure no QBO calls are invoked because it's past TTL
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_qbo_access_token",
        lambda: (_ for _ in ()).throw(RuntimeError("Should not be called")),
    )
    result, status = retry_unpaid_bookings(None)
    assert status == 200
    assert result["processed"] == 0
    assert result["retries"] == 0
    assert ref.updates == []


def test_qbo_payment_link_recovery_success(monkeypatch):
    from retry_unpaid_bookings import datetime
    import datetime as dt_mod
    now_val = datetime.now(dt_mod.timezone.utc)

    tour_dt = now_val + dt_mod.timedelta(days=10) # lead time >= 7 days -> TTL = 48 hours
    created_at = now_val - dt_mod.timedelta(hours=2) # 2 hours old, active

    data = {
        "retry_attempts": 1,
        "email_sent_count": 0,
        "guest": {"email": "guest@example.com", "name": "Guest"},
        "tour_datetime": tour_dt,
        "created_at": created_at,
        "party_size": 2,
        "integration_ids": {"qbo_invoice_id": "inv_999"},
        "payment_link": "", # missing link!
    }
    ref = MockReference()
    doc = MockDoc(data, ref)
    mock_collection = make_mock_firestore([doc])
    monkeypatch.setattr(
        firestore,
        "Client",
        lambda **kwargs: mock.MagicMock(collection=lambda name: mock_collection),
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_qbo_access_token", lambda: ("qbo_token", "realm_123")
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings.retrieve_qbo_invoice_link",
        lambda token, realm, inv_id: "https://pay.recovered.com/inv_999",
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

    # Verify updated payment_link in database
    update_payloads = ref.updates
    assert any(
        "payment_link" in upd and upd["payment_link"] == "https://pay.recovered.com/inv_999"
        for upd in update_payloads
    )


def test_qbo_payment_link_recovery_failure(monkeypatch):
    from retry_unpaid_bookings import datetime
    import datetime as dt_mod
    now_val = datetime.now(dt_mod.timezone.utc)

    tour_dt = now_val + dt_mod.timedelta(days=10)
    created_at = now_val - dt_mod.timedelta(hours=2)

    data = {
        "retry_attempts": 1,
        "email_sent_count": 0,
        "guest": {"email": "guest@example.com", "name": "Guest"},
        "tour_datetime": tour_dt,
        "created_at": created_at,
        "party_size": 2,
        "integration_ids": {"qbo_invoice_id": "inv_999"},
        "payment_link": " ", # empty string/whitespace
    }
    ref = MockReference()
    doc = MockDoc(data, ref)
    mock_collection = make_mock_firestore([doc])
    monkeypatch.setattr(
        firestore,
        "Client",
        lambda **kwargs: mock.MagicMock(collection=lambda name: mock_collection),
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings.get_qbo_access_token", lambda: ("qbo_token", "realm_123")
    )
    monkeypatch.setattr(
        "retry_unpaid_bookings.retrieve_qbo_invoice_link",
        lambda token, realm, inv_id: None, # failed to recover!
    )

    result, status = retry_unpaid_bookings(None)
    assert status == 200
    assert result["processed"] == 0
    assert result["retries"] == 0
    assert result["emails_sent"] == 0

    # Verify retry_attempts incremented
    update_payloads = ref.updates
    assert any(
        "retry_attempts" in upd and upd["retry_attempts"] == 2
        for upd in update_payloads
    )


@mock.patch("requests.post")
def test_send_temp_issue_email_html_escaping(mock_post, monkeypatch):
    from retry_unpaid_bookings import _send_temp_issue_email

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 202
    mock_post.return_value = mock_resp

    res = _send_temp_issue_email(
        "access_token",
        "user_id",
        "booking_123<script>alert(1)</script>",
        "guest@example.com",
        "Guest <img src=x onerror=alert(2)>",
        "2026-06-15",
        "10:00",
    )
    assert res is True

    # Inspect requests.post call
    mock_post.assert_called_once()
    post_kwargs = mock_post.call_args[1]
    posted_json = post_kwargs["json"]
    email_body = posted_json["message"]["body"]["content"]

    # Verify that the injection payloads are fully HTML-escaped!
    assert "<script>" not in email_body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in email_body
    assert "<img" not in email_body
    assert "Guest &lt;img src=x onerror=alert(2)&gt;" in email_body


@mock.patch("requests.get")
def test_retrieve_qbo_invoice_link_api(mock_get, monkeypatch):
    from retry_unpaid_bookings import retrieve_qbo_invoice_link

    # Force environment to 'sandbox' and non-mock test to bypass mock bypass
    monkeypatch.setenv("QBO_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("QBO_BASE_URL", "https://sandbox-quickbooks.api.intuit.com/v3/company")

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Invoice": {
            "InvoiceLink": "https://sandbox.qbo.intuit.com/app/invoice?id=123"
        }
    }
    mock_get.return_value = mock_resp

    # Force _get_db to return None to bypass the MockDB check
    monkeypatch.setattr("retry_unpaid_bookings._get_db", lambda: None)

    link = retrieve_qbo_invoice_link("real_token", "real_realm", "123")
    assert link == "https://sandbox.qbo.intuit.com/app/invoice?id=123"

    # Verify requests.get parameters
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert args[0] == "https://sandbox-quickbooks.api.intuit.com/v3/company/real_realm/invoice/123"
    assert kwargs["params"] == {"include": "invoiceLink", "minorversion": "75"}
    assert kwargs["headers"]["Authorization"] == "Bearer real_token"


@mock.patch("requests.get")
def test_retrieve_qbo_invoice_link_api_failure(mock_get, monkeypatch):
    from retry_unpaid_bookings import retrieve_qbo_invoice_link
    monkeypatch.setattr("retry_unpaid_bookings._get_db", lambda: None)
    mock_get.side_effect = Exception("HTTP connection timeout")

    link = retrieve_qbo_invoice_link("real_token", "real_realm", "123")
    assert link is None

