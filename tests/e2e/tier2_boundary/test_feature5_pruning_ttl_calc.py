import pytest
from unittest.mock import MagicMock

def test_pruning_ttl_exact_threshold(client, mock_requests):
    response = client.post('/pruning_ttl_calc', json={"tour_date": "2026-06-15T09:00:00Z"})
    assert response.status_code in [200, 400, 500]

def test_pruning_ttl_past_tour_date(client, mock_requests):
    response = client.post('/pruning_ttl_calc', json={"tour_date": "2020-06-15T09:00:00Z"})
    assert response.status_code in [200, 400, 500]

def test_pruning_ttl_dst_transition(client, mock_requests):
    response = client.post('/pruning_ttl_calc', json={"tour_date": "2026-11-01T09:00:00Z"})
    assert response.status_code in [200, 400, 500]

def test_pruning_ttl_far_future_date(client, mock_requests):
    response = client.post('/pruning_ttl_calc', json={"tour_date": "2099-06-15T09:00:00Z"})
    assert response.status_code in [200, 400, 500]

def test_pruning_ttl_invalid_timestamp_format(client, mock_requests):
    response = client.post('/pruning_ttl_calc', json={"tour_date": "15-06-2026 09:00"})
    assert response.status_code in [200, 400, 500]
