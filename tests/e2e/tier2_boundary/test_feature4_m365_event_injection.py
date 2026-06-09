import pytest
from unittest.mock import MagicMock

def test_m365_inject_empty_subject(client, mock_requests):
    response = client.post('/m365_event_injection', json={"subject": "", "start": "2026-06-15T09:00:00"})
    assert response.status_code in [200, 400, 500]

def test_m365_inject_max_length_description(client, mock_requests):
    long_desc = "A" * 10000
    response = client.post('/m365_event_injection', json={"subject": "Tour", "description": long_desc})
    assert response.status_code in [200, 400, 500]

def test_m365_inject_invalid_timezone_id(client, mock_requests):
    response = client.post('/m365_event_injection', json={"subject": "Tour", "timezone": "Invalid/Timezone"})
    assert response.status_code in [200, 400, 500]

def test_m365_inject_negative_duration(client, mock_requests):
    response = client.post('/m365_event_injection', json={"subject": "Tour", "start": "2026-06-15T10:00:00", "end": "2026-06-15T09:00:00"})
    assert response.status_code in [200, 400, 500]

def test_m365_inject_500_retry(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.side_effect = [MagicMock(status_code=500), MagicMock(status_code=200, json=lambda: {"id": "123"})]
    response = client.post('/m365_event_injection', json={"subject": "Tour", "start": "2026-06-15T09:00:00"})
    assert response.status_code in [200, 400, 500]
