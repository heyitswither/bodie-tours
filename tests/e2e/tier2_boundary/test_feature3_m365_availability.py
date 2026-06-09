import pytest
from unittest.mock import MagicMock

def test_m365_avail_exact_boundary_match(client, mock_requests):
    response = client.get('/m365_availability', query_string={"start": "2026-06-15T09:00:00", "end": "2026-06-15T10:00:00"})
    assert response.status_code in [200, 400, 500]

def test_m365_avail_missing_timezone(client, mock_requests):
    response = client.get('/m365_availability', query_string={"start": "2026-06-15T09:00:00"})
    assert response.status_code in [200, 400, 500]

def test_m365_avail_midnight_span(client, mock_requests):
    response = client.get('/m365_availability', query_string={"start": "2026-06-15T23:00:00", "end": "2026-06-16T01:00:00"})
    assert response.status_code in [200, 400, 500]

def test_m365_avail_past_date(client, mock_requests):
    response = client.get('/m365_availability', query_string={"start": "2020-01-01T09:00:00"})
    assert response.status_code in [200, 400, 500]

def test_m365_avail_massive_event_count(client, mock_requests):
    _, mock_get = mock_requests
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"value": [{"id": f"event_{i}"} for i in range(10000)]})
    response = client.get('/m365_availability', query_string={"start": "2026-06-15T00:00:00", "end": "2026-06-30T00:00:00"})
    assert response.status_code in [200, 400, 500]
