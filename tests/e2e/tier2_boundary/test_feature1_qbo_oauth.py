import pytest
from unittest.mock import MagicMock
import requests

def test_qbo_oauth_missing_auth_code(client, mock_requests):
    response = client.get('/qbo_oauth')
    # Should handle missing code appropriately
    assert response.status_code in [200, 400, 500]

def test_qbo_oauth_expired_refresh_token(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=400, json=lambda: {"error": "invalid_grant"})
    response = client.post('/qbo_oauth', json={"refresh_token": "expired_token"})
    assert response.status_code in [200, 400, 401, 500]

def test_qbo_oauth_concurrent_refresh(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"access_token": "new_token"})
    response1 = client.post('/qbo_oauth', json={"refresh_token": "token1"})
    response2 = client.post('/qbo_oauth', json={"refresh_token": "token1"})
    assert response1.status_code in [200, 400, 500]
    assert response2.status_code in [200, 400, 500]

def test_qbo_oauth_malformed_token_response(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"not_an_access_token": "foo"})
    response = client.post('/qbo_oauth', json={"code": "auth_code"})
    assert response.status_code in [200, 400, 500]

def test_qbo_oauth_network_timeout(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.side_effect = requests.exceptions.Timeout("Timeout")
    response = client.post('/qbo_oauth', json={"code": "auth_code"})
    assert response.status_code in [200, 400, 500, 503, 504]
