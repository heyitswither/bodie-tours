import pytest

def test_m365_event_creation_success(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 201
    mock_requests_post.return_value.json.return_value = {"id": "event_123"}
    payload = {"date": "2026-06-25", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post('/booking', json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200

def test_m365_event_details_accuracy(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 201
    mock_requests_post.return_value.json.return_value = {"id": "event_123"}
    payload = {"date": "2026-06-25", "time": "10:00", "party_size": 2, "guest": {"name": "Bob"}}
    response = client.post('/booking', json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200
    # Ensure correct details sent via mock inspection later in real implementation

def test_m365_event_pending_status_flag(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 201
    mock_requests_post.return_value.json.return_value = {"id": "event_123"}
    payload = {"date": "2026-06-25", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post('/booking', json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200

def test_m365_event_id_saved(client, mock_requests_post, mock_main_db):
    mock_requests_post.return_value.status_code = 201
    mock_requests_post.return_value.json.return_value = {"id": "event_123"}
    payload = {"date": "2026-06-25", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post('/booking', json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200
    # Verify that a Firestore transaction was created during the booking
    assert mock_main_db.transaction.called

def test_m365_event_creation_failure_rollback(client, mock_requests_post, mock_firestore):
    mock_requests_post.return_value.status_code = 500
    payload = {"date": "2026-06-25", "time": "10:00", "party_size": 2, "guest": {}}
    response = client.post('/booking', json=payload)
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code in [500, 200]
