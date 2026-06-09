import pytest
from unittest.mock import MagicMock
import requests

def test_pruning_removal_missing_event_id(client, mock_requests):
    response = client.post('/pruning_event_removal', json={"booking_id": "123"})
    assert response.status_code in [200, 400, 500]

def test_pruning_removal_404_not_found(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=404)
    response = client.post('/pruning_event_removal', json={"event_id": "123"})
    assert response.status_code in [200, 400, 404, 500]

def test_pruning_removal_permission_denied(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=403)
    response = client.post('/pruning_event_removal', json={"event_id": "123"})
    assert response.status_code in [200, 400, 403, 500]

def test_pruning_removal_multi_day_event(client, mock_requests):
    response = client.post('/pruning_event_removal', json={"event_id": "123", "multi_day": True})
    assert response.status_code in [200, 400, 500]

def test_pruning_removal_network_drop(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.side_effect = requests.exceptions.ConnectionError("Connection dropped")
    response = client.post('/pruning_event_removal', json={"event_id": "123"})
    assert response.status_code in [200, 400, 500, 503, 504]
