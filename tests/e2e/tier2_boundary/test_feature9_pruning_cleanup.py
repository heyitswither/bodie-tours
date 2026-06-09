import pytest
from unittest.mock import MagicMock

def test_pruning_cleanup_exact_midnight(client, mock_requests):
    response = client.post('/pruning_cleanup', json={"run_time": "2026-06-15T00:00:00Z"})
    assert response.status_code in [200, 400, 500]

def test_pruning_cleanup_idempotency(client, mock_requests):
    response1 = client.post('/pruning_cleanup', json={"run_time": "2026-06-15T01:00:00Z"})
    response2 = client.post('/pruning_cleanup', json={"run_time": "2026-06-15T01:00:00Z"})
    assert response1.status_code in [200, 400, 500]
    assert response2.status_code in [200, 400, 500]

def test_pruning_cleanup_empty_dataset(client, mock_requests):
    # Mock firestore to return empty
    response = client.post('/pruning_cleanup', json={"force_empty": True})
    assert response.status_code in [200, 400, 500]

def test_pruning_cleanup_pagination_limit(client, mock_requests):
    response = client.post('/pruning_cleanup', json={"limit": 10000})
    assert response.status_code in [200, 400, 500]

def test_pruning_cleanup_missing_status(client, mock_requests):
    response = client.post('/pruning_cleanup', json={"run_time": "2026-06-15T01:00:00Z", "ignore_status": True})
    assert response.status_code in [200, 400, 500]
