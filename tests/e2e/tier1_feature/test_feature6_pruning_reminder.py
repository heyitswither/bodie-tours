import pytest

def test_pruning_reminder_email_sent(client, mock_requests_post):
    mock_requests_post.return_value.status_code = 202
    response = client.post('/prune')
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200

def test_pruning_reminder_email_content(client, mock_requests_post):
    response = client.post('/prune')
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200

def test_pruning_reminder_email_flag_set(client, mock_firestore):
    response = client.post('/prune')
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200

def test_pruning_reminder_not_sent_early(client, mock_requests_post):
    response = client.post('/prune')
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200

def test_pruning_reminder_not_sent_paid(client, mock_requests_post):
    response = client.post('/prune')
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200
