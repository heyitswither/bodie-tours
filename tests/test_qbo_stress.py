import pytest
from unittest.mock import patch, MagicMock
from flask import Request
from werkzeug.test import EnvironBuilder
import os
import requests

import sys

# Mock functions framework and firestore to allow import of main
class DummyFunctionsFramework:
    @staticmethod
    def http(func):
        return func

if 'functions_framework' not in sys.modules:
    sys.modules['functions_framework'] = DummyFunctionsFramework

mock_firestore = MagicMock()
mock_firestore.transactional = lambda f: f
if 'google.cloud.firestore' not in sys.modules:
    sys.modules['google.cloud.firestore'] = mock_firestore
    sys.modules['google.cloud'] = MagicMock(firestore=mock_firestore)

import main

@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {
        'QBO_CLIENT_ID': 'test_client_id',
        'QBO_CLIENT_SECRET': 'test_client_secret',
        'QBO_REDIRECT_URI': 'https://example.com/callback',
        'QBO_ENVIRONMENT': 'sandbox'
    }):
        yield

@patch('main.db')
@patch('requests.post')
def test_qbo_callback_csrf_protection(mock_post, mock_db, mock_env):
    """
    Security test: verify CSRF protection is in place.
    The callback must reject requests where the state parameter does not match
    the cookie, preventing cross-site request forgery attacks.
    """
    # Simulate an attacker providing a code but an invalid/missing state
    builder = EnvironBuilder(method='GET', query_string={'code': 'attacker_code', 'realmId': '123456', 'state': 'attacker_manipulated_state'})
    env = builder.get_environ()
    request = Request(env)
    # No matching cookie set — state mismatch simulates CSRF attack

    mock_response = MagicMock()
    mock_response.json.return_value = {
        'access_token': 'test_access_token',
        'refresh_token': 'test_refresh_token',
        'expires_in': 3600
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    mock_doc = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc

    response, status_code = main.qbo_callback(request)

    # CSRF protection is active: mismatched/missing state must be rejected
    assert status_code == 400, "CSRF protection missing! State parameter is not being validated."
    assert response['status'] == 'error'
    assert response['message'] == 'Invalid state parameter'
