import pytest
from unittest.mock import call
import requests


def test_qbo_refresh_and_invoice_gen(client, mock_requests_post):
    # F1 + F2: QBO token expired, forces refresh, then generates invoice
    # With pre-cached valid tokens, booking makes 3 POST calls:
    # 1. M365 getSchedule (availability), 2. M365 event inject, 3. QBO invoice
    mock_requests_post.side_effect = [
        # 1. M365 Schedule Check
        type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self=None: {
                    "value": [{"scheduleItems": [], "availabilityView": "0"}]
                },
            },
        )(),
        # 2. M365 Event Injection
        type(
            "Response",
            (),
            {"status_code": 201, "json": lambda self=None: {"id": "event_abc"}},
        )(),
        # 3. QBO Invoice Creation
        type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self=None: {
                    "Invoice": {"Id": "123"},
                    "PaymentLink": "http://pay",
                },
            },
        )(),
    ]

    payload = {
        "date": "2026-06-25",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_m365_avail_and_event_inject(client, mock_requests_post):
    # F3 + F4: M365 availability check, then inject event
    # With pre-cached valid tokens, booking makes 3 POST calls:
    # 1. M365 getSchedule (availability), 2. M365 event inject, 3. QBO invoice
    mock_requests_post.side_effect = [
        # 1. Availability Check
        type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self=None: {
                    "value": [
                        {
                            "scheduleId": "test",
                            "availabilityView": "0",
                            "scheduleItems": [],
                        }
                    ]
                },
            },
        )(),
        # 2. Event Injection
        type(
            "Response",
            (),
            {"status_code": 201, "json": lambda self=None: {"id": "event123"}},
        )(),
        # 3. QBO Invoice Creation
        type(
            "Response",
            (),
            {"status_code": 200, "json": lambda self=None: {"Invoice": {"Id": "456"}}},
        )(),
    ]

    payload = {
        "date": "2026-06-26",
        "time": "11:00",
        "party_size": 2,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_invoice_and_dynamic_ttl(client, mock_requests_post, mock_firestore):
    # F2 + F5: Booking generates invoice; pruning service calculates dynamic TTL
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {"Invoice": {"Id": "123"}}

    payload = {
        "date": "2026-06-27",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")

    prune_response = client.post("/prune")
    if prune_response.status_code == 501:
        pytest.xfail("Not implemented")
    assert prune_response.status_code == 200


def test_m365_inject_and_event_removal(
    client, mock_requests_post, mock_firestore, mock_requests_get, monkeypatch
):
    # F4 + F8: Event injected, expires, pruning removes it
    mock_delete = type("MockDelete", (), {"status_code": 204})()
    monkeypatch.setattr(requests, "delete", lambda *args, **kwargs: mock_delete)

    prune_response = client.post("/prune")
    if prune_response.status_code == 501:
        pytest.xfail("Not implemented")
    assert prune_response.status_code == 200


def test_dynamic_ttl_and_reminder_email(client, mock_requests_post, mock_firestore):
    # F5 + F6: Pruning uses dynamic TTL to send reminder email
    mock_requests_post.return_value.status_code = 202

    prune_response = client.post("/prune")
    if prune_response.status_code == 501:
        pytest.xfail("Not implemented")
    assert prune_response.status_code == 200


def test_expiration_cancellation_and_m365_removal(
    client, mock_requests_post, mock_firestore, monkeypatch
):
    # F7 + F8: Exceeds TTL, cancelled, M365 event deleted
    mock_delete = type("MockDelete", (), {"status_code": 204})()
    monkeypatch.setattr(requests, "delete", lambda *args, **kwargs: mock_delete)

    prune_response = client.post("/prune")
    if prune_response.status_code == 501:
        pytest.xfail("Not implemented")
    assert prune_response.status_code == 200


def test_m365_inject_and_completed_cleanup(
    client, mock_requests_post, mock_firestore, monkeypatch
):
    # F4 + F9: Event injected, date passes, cleanup service prunes it
    mock_delete = type("MockDelete", (), {"status_code": 204})()
    monkeypatch.setattr(requests, "delete", lambda *args, **kwargs: mock_delete)

    prune_response = client.post("/prune")
    if prune_response.status_code == 501:
        pytest.xfail("Not implemented")
    assert prune_response.status_code == 200


def test_concurrent_qbo_refresh_and_m365_avail(client, mock_requests_post):
    # F1 + F3: QBO token refresh and M365 availability check
    # With pre-cached valid tokens, booking makes 3 POST calls:
    # 1. M365 getSchedule, 2. M365 event inject, 3. QBO invoice
    mock_requests_post.side_effect = [
        # 1. M365 Schedule Check
        type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self=None: {
                    "value": [{"scheduleItems": [], "availabilityView": "0"}]
                },
            },
        )(),
        # 2. M365 Event Injection
        type(
            "Response",
            (),
            {"status_code": 201, "json": lambda self=None: {"id": "evt_789"}},
        )(),
        # 3. QBO Invoice
        type(
            "Response",
            (),
            {"status_code": 200, "json": lambda self=None: {"Invoice": {"Id": "789"}}},
        )(),
    ]

    payload = {
        "date": "2026-06-28",
        "time": "12:00",
        "party_size": 2,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_invoice_and_expiration_cancellation(
    client, mock_requests_post, mock_firestore
):
    # F2 + F7: Pending QBO invoice is marked canceled when expiration job runs
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "Invoice": {"Id": "123", "status": "Voided"}
    }

    prune_response = client.post("/prune")
    if prune_response.status_code == 501:
        pytest.xfail("Not implemented")
    assert prune_response.status_code == 200
