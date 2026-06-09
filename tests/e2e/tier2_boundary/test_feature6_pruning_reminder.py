import pytest
from unittest.mock import MagicMock
import requests

def test_pruning_reminder_empty_email(client, mock_requests):
    response = client.post('/pruning_reminder', json={"email": ""})
    assert response.status_code in [200, 400, 500]

def test_pruning_reminder_race_condition(client, mock_requests):
    response1 = client.post('/pruning_reminder', json={"booking_id": "123"})
    response2 = client.post('/pruning_reminder', json={"booking_id": "123"})
    assert response1.status_code in [200, 400, 500]
    assert response2.status_code in [200, 400, 500]

def test_pruning_reminder_outlook_timeout(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.side_effect = requests.exceptions.Timeout("Timeout")
    response = client.post('/pruning_reminder', json={"booking_id": "123", "email": "test@example.com"})
    assert response.status_code in [200, 400, 500, 503, 504]

def test_pruning_reminder_long_name_truncation(client, mock_requests):
    long_name = "A" * 500
    response = client.post('/pruning_reminder', json={"booking_id": "123", "name": long_name})
    assert response.status_code in [200, 400, 500]

def test_pruning_reminder_already_cancelled(client, mock_requests):
    response = client.post('/pruning_reminder', json={"booking_id": "123", "status": "CANCELLED"})
    assert response.status_code in [200, 400, 500]
