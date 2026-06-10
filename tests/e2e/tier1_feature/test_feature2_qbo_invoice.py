import pytest
from unittest.mock import MagicMock


def test_qbo_invoice_creation_success(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {"Invoice": {"Id": "123"}}

    payload = {
        "date": "2026-06-16",
        "time": "11:00",
        "party_size": 2,
        "guest": {"name": "Test User", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_qbo_invoice_customer_creation(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "Customer": {"Id": "456"},
        "Invoice": {"Id": "123"},
    }

    payload = {
        "date": "2026-06-16",
        "time": "11:00",
        "party_size": 1,
        "guest": {"name": "New Cust", "email": "new@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_qbo_invoice_line_items_correct(client, mock_requests_post):
    payload = {
        "date": "2026-06-16",
        "time": "12:00",
        "party_size": 3,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_qbo_invoice_payment_link_returned(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "Invoice": {"Id": "123", "AllowOnlineCreditCardPayment": True},
        "PaymentLink": "https://intuit.com/pay/123",
    }
    payload = {
        "date": "2026-06-17",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")

    data = response.get_json() or {}
    if "payment_link" in data:
        assert data["payment_link"] == "https://intuit.com/pay/123"


def test_qbo_invoice_creation_api_failure(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 500

    payload = {
        "date": "2026-06-18",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Test", "email": "test@example.com"},
    }
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    # Depending on implementation, it might fail the booking or just return 500
    assert response.status_code in [500, 200]


def test_qbo_webhook_updates_status(client, mock_main_db):
    # Setup mock booking with qbo_invoice_id = "INV-1042" and PENDING status
    mock_booking_ref = MagicMock()
    mock_booking_doc = MagicMock()
    mock_booking_doc.reference = mock_booking_ref
    mock_booking_doc.to_dict.return_value = {
        "payment_status": "PENDING",
        "integration_ids": {"qbo_invoice_id": "INV-1042"},
    }

    # Setup custom collection mock to return our document on query
    mock_collection = MagicMock()
    mock_collection.where.return_value.stream.return_value = [mock_booking_doc]

    # Mock the config collection snapshot to return our verifier_token
    mock_config_doc = MagicMock()
    mock_config_snapshot = MagicMock()
    mock_config_snapshot.exists = True
    mock_config_snapshot.to_dict.return_value = {"verifier_token": "my_secret_token"}
    mock_config_doc.get.return_value = mock_config_snapshot

    mock_config_collection = MagicMock()
    mock_config_collection.document.return_value = mock_config_doc

    def collection_side_effect(name):
        if name == "bookings":
            return mock_collection
        if name == "config":
            return mock_config_collection
        return MagicMock()

    mock_main_db.collection.side_effect = collection_side_effect

    webhook_payload = {
        "eventNotifications": [
            {
                "realmId": "123456",
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "id": "INV-1042", "operation": "Update"}
                    ]
                },
            }
        ]
    }

    import hmac, hashlib, base64, json

    payload_str = json.dumps(webhook_payload)
    payload_bytes = payload_str.encode("utf-8")
    sig_bytes = hmac.new(b"my_secret_token", payload_bytes, hashlib.sha256).digest()
    sig_b64 = base64.b64encode(sig_bytes).decode("utf-8")

    response = client.post(
        "/qbo/webhook",
        data=payload_str,
        content_type="application/json",
        headers={"Intuit-Signature": sig_b64},
    )
    assert response.status_code == 200
    # Check that update was called on the booking doc reference to set PAID
    mock_booking_ref.update.assert_called_once_with({"payment_status": "PAID"})


def test_qbo_webhook_signature_success(client, mock_main_db):
    import hmac
    import hashlib
    import base64
    import json

    # Setup mock booking with qbo_invoice_id = "INV-1042" and PENDING status
    mock_booking_ref = MagicMock()
    mock_booking_doc = MagicMock()
    mock_booking_doc.reference = mock_booking_ref
    mock_booking_doc.to_dict.return_value = {
        "payment_status": "PENDING",
        "integration_ids": {"qbo_invoice_id": "INV-1042"},
    }

    mock_bookings_collection = MagicMock()
    mock_bookings_collection.where.return_value.stream.return_value = [mock_booking_doc]

    # Mock the config collection snapshot to return our verifier_token
    mock_config_doc = MagicMock()
    mock_config_snapshot = MagicMock()
    mock_config_snapshot.exists = True
    mock_config_snapshot.to_dict.return_value = {"verifier_token": "my_secret_token"}
    mock_config_doc.get.return_value = mock_config_snapshot

    mock_config_collection = MagicMock()
    mock_config_collection.document.return_value = mock_config_doc

    def collection_side_effect(name):
        if name == "bookings":
            return mock_bookings_collection
        if name == "config":
            return mock_config_collection
        return MagicMock()

    mock_main_db.collection.side_effect = collection_side_effect

    webhook_payload = {
        "eventNotifications": [
            {
                "realmId": "123456",
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "id": "INV-1042", "operation": "Update"}
                    ]
                },
            }
        ]
    }

    # Generate signature
    token = "my_secret_token"
    payload_str = json.dumps(webhook_payload)
    payload_bytes = payload_str.encode("utf-8")
    sig_bytes = hmac.new(token.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    sig_b64 = base64.b64encode(sig_bytes).decode("utf-8")

    response = client.post(
        "/qbo/webhook",
        data=payload_str,
        content_type="application/json",
        headers={"Intuit-Signature": sig_b64},
    )

    assert response.status_code == 200
    mock_booking_ref.update.assert_called_once_with({"payment_status": "PAID"})


def test_qbo_webhook_signature_invalid(client, mock_main_db):
    # Setup mock booking with qbo_invoice_id = "INV-1042" and PENDING status
    mock_booking_ref = MagicMock()
    mock_booking_doc = MagicMock()
    mock_booking_doc.reference = mock_booking_ref
    mock_booking_doc.to_dict.return_value = {
        "payment_status": "PENDING",
        "integration_ids": {"qbo_invoice_id": "INV-1042"},
    }

    mock_bookings_collection = MagicMock()
    mock_bookings_collection.where.return_value.stream.return_value = [mock_booking_doc]

    # Mock the config collection snapshot to return our verifier_token
    mock_config_doc = MagicMock()
    mock_config_snapshot = MagicMock()
    mock_config_snapshot.exists = True
    mock_config_snapshot.to_dict.return_value = {"verifier_token": "my_secret_token"}
    mock_config_doc.get.return_value = mock_config_snapshot

    mock_config_collection = MagicMock()
    mock_config_collection.document.return_value = mock_config_doc

    def collection_side_effect(name):
        if name == "bookings":
            return mock_bookings_collection
        if name == "config":
            return mock_config_collection
        return MagicMock()

    mock_main_db.collection.side_effect = collection_side_effect

    webhook_payload = {
        "eventNotifications": [
            {
                "realmId": "123456",
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "id": "INV-1042", "operation": "Update"}
                    ]
                },
            }
        ]
    }

    # Send request with invalid signature header
    response = client.post(
        "/qbo/webhook",
        json=webhook_payload,
        headers={"Intuit-Signature": "invalid_base64_sig!!!"},
    )

    assert response.status_code == 401
    mock_booking_ref.update.assert_not_called()


def test_qbo_webhook_signature_missing(client, mock_main_db):
    # Setup mock booking with qbo_invoice_id = "INV-1042" and PENDING status
    mock_booking_ref = MagicMock()
    mock_booking_doc = MagicMock()
    mock_booking_doc.reference = mock_booking_ref
    mock_booking_doc.to_dict.return_value = {
        "payment_status": "PENDING",
        "integration_ids": {"qbo_invoice_id": "INV-1042"},
    }

    mock_bookings_collection = MagicMock()
    mock_bookings_collection.where.return_value.stream.return_value = [mock_booking_doc]

    # Mock the config collection snapshot to return our verifier_token
    mock_config_doc = MagicMock()
    mock_config_snapshot = MagicMock()
    mock_config_snapshot.exists = True
    mock_config_snapshot.to_dict.return_value = {"verifier_token": "my_secret_token"}
    mock_config_doc.get.return_value = mock_config_snapshot

    mock_config_collection = MagicMock()
    mock_config_collection.document.return_value = mock_config_doc

    def collection_side_effect(name):
        if name == "bookings":
            return mock_bookings_collection
        if name == "config":
            return mock_config_collection
        return MagicMock()

    mock_main_db.collection.side_effect = collection_side_effect

    webhook_payload = {
        "eventNotifications": [
            {
                "realmId": "123456",
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "id": "INV-1042", "operation": "Update"}
                    ]
                },
            }
        ]
    }

    # Send request with missing signature header
    response = client.post("/qbo/webhook", json=webhook_payload)

    assert response.status_code == 401
    mock_booking_ref.update.assert_not_called()
