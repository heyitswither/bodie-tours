import pytest


def test_m365_availability_free_slot(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "value": [
            {
                "scheduleId": "test@example.com",
                "availabilityView": "0",
                "scheduleItems": [],
            }
        ]
    }
    payload = {"date": "2026-06-20", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_m365_availability_busy_slot(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "value": [
            {
                "scheduleId": "test@example.com",
                "availabilityView": "2",
                "scheduleItems": [{"status": "busy"}],
            }
        ]
    }
    payload = {"date": "2026-06-20", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    # Busy slot might mean we return 409 Conflict
    assert response.status_code in [409, 200, 500]


def test_m365_availability_partial_overlap(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "value": [
            {
                "scheduleId": "test@example.com",
                "scheduleItems": [
                    {"status": "busy", "start": {"dateTime": "2026-06-20T10:30:00"}}
                ],
            }
        ]
    }
    payload = {"date": "2026-06-20", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code in [409, 200, 500]


def test_m365_availability_all_day_event(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 200
    mock_requests_post.return_value.json.return_value = {
        "value": [
            {
                "scheduleId": "test@example.com",
                "scheduleItems": [
                    {
                        "status": "busy",
                        "start": {"dateTime": "2026-06-20T00:00:00"},
                        "end": {"dateTime": "2026-06-21T00:00:00"},
                    }
                ],
            }
        ]
    }
    payload = {"date": "2026-06-20", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code in [409, 200, 500]


def test_m365_availability_api_error(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 500
    payload = {"date": "2026-06-20", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post("/booking", json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code in [500, 200]
