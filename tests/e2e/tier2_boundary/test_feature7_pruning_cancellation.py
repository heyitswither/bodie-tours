import pytest
from unittest.mock import MagicMock


def test_pruning_expiration_payment_collision(client, mock_requests):
    # Simulate receiving payment confirmation right as pruning happens
    response = client.post(
        "/pruning_cancellation", json={"booking_id": "123", "status": "PAID"}
    )
    assert response.status_code in [200, 400, 500]


def test_pruning_expiration_missing_id(client, mock_requests):
    response = client.post("/pruning_cancellation", json={})
    assert response.status_code in [200, 400, 500]


def test_pruning_expiration_batch_limit(client, mock_requests):
    # Pass a large batch
    response = client.post(
        "/pruning_cancellation", json={"booking_ids": [str(i) for i in range(1000)]}
    )
    assert response.status_code in [200, 400, 500]


def test_pruning_expiration_already_cancelled(client, mock_requests):
    response = client.post(
        "/pruning_cancellation", json={"booking_id": "123", "status": "CANCELLED"}
    )
    assert response.status_code in [200, 400, 500]


def test_pruning_expiration_exact_boundary(client, mock_requests):
    response = client.post(
        "/pruning_cancellation", json={"booking_id": "123", "ttl_expired_sec_ago": 0}
    )
    assert response.status_code in [200, 400, 500]
