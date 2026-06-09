import pytest
from unittest.mock import patch, MagicMock
import sys
sys.path.insert(0, '.')
from flask import Request
from werkzeug.test import EnvironBuilder
import os
import requests

import sys
from unittest.mock import patch, MagicMock

class DummyFunctionsFramework:
    @staticmethod
    def http(func):
        return func

sys.modules['functions_framework'] = DummyFunctionsFramework

mock_firestore = MagicMock()
mock_firestore.transactional = lambda f: f
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

def test_qbo_login(mock_env):
    builder = EnvironBuilder(method='GET')
    env = builder.get_environ()
    request = Request(env)

    response = main.qbo_login(request)
    
    assert response.status_code == 302
    assert response.location.startswith('https://appcenter.intuit.com/connect/oauth2?')
    assert 'client_id=test_client_id' in response.location
    assert 'redirect_uri=https%3A%2F%2Fexample.com%2Fcallback' in response.location
    assert 'response_type=code' in response.location
    assert 'scope=com.intuit.quickbooks.accounting' in response.location
    assert 'state=' in response.location
    
    cookies = response.headers.getlist('Set-Cookie')
    assert any('qbo_oauth_state=' in cookie for cookie in cookies)

@patch('main.db')
@patch('requests.post')
def test_qbo_callback_success(mock_post, mock_db, mock_env):
    builder = EnvironBuilder(method='GET', query_string={'code': 'test_auth_code', 'realmId': '123456', 'state': 'qbo_auth_state_xyz123'})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {'qbo_oauth_state': 'qbo_auth_state_xyz123'}

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

    assert status_code == 200
    assert response['status'] == 'success'
    
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
    assert 'Authorization' in kwargs['headers']
    assert kwargs['data']['grant_type'] == 'authorization_code'
    assert kwargs['data']['code'] == 'test_auth_code'
    
    mock_db.collection.assert_called_with('config')
    mock_db.collection.return_value.document.assert_called_with('qbo_auth')
    mock_doc.set.assert_called_once()
    set_args = mock_doc.set.call_args[0][0]
    assert set_args['access_token'] == 'test_access_token'
    assert set_args['refresh_token'] == 'test_refresh_token'
    assert set_args['realmId'] == '123456'

def test_qbo_callback_missing_code(mock_env):
    builder = EnvironBuilder(method='GET', query_string={'state': 'valid_state'})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {'qbo_oauth_state': 'valid_state'}

    response, status_code = main.qbo_callback(request)

    assert status_code == 400
    assert response['status'] == 'error'

@patch('requests.post')
def test_qbo_callback_request_exception(mock_post, mock_env):
    builder = EnvironBuilder(method='GET', query_string={'code': 'test_auth_code', 'state': 'valid_state'})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {'qbo_oauth_state': 'valid_state'}

    mock_post.side_effect = requests.exceptions.RequestException("Test error")

    response, status_code = main.qbo_callback(request)

    assert status_code == 500
    assert response['status'] == 'error'
    assert 'Failed to exchange token' in response['message']

def test_qbo_callback_invalid_state(mock_env):
    # Test missing state in args
    builder = EnvironBuilder(method='GET', query_string={'code': 'test_code'})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {'qbo_oauth_state': 'some_state'}
    response, status = main.qbo_callback(request)
    assert status == 400
    assert response['message'] == 'Invalid state parameter'

    # Test missing state in cookies
    builder = EnvironBuilder(method='GET', query_string={'code': 'test_code', 'state': 'some_state'})
    env = builder.get_environ()
    request = Request(env)
    response, status = main.qbo_callback(request)
    assert status == 400
    assert response['message'] == 'Invalid state parameter'

    # Test mismatching state
    builder = EnvironBuilder(method='GET', query_string={'code': 'test_code', 'state': 'state1'})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {'qbo_oauth_state': 'state2'}
    response, status = main.qbo_callback(request)
    assert status == 400
    assert response['message'] == 'Invalid state parameter'

def test_qbo_login_db_exception(mock_env):
    with patch('main.db') as mock_db:
        mock_db.__class__.__name__ = 'FirestoreClient'
        mock_doc = MagicMock()
        mock_doc.get.side_effect = Exception("Firestore error")
        mock_db.collection.return_value.document.return_value = mock_doc
        
        builder = EnvironBuilder(method='GET')
        env = builder.get_environ()
        request = Request(env)

        response = main.qbo_login(request)
        assert response.status_code == 302
        assert 'client_id=test_client_id' in response.location

def test_qbo_callback_db_exception(mock_env):
    with patch('main.db') as mock_db:
        mock_db.__class__.__name__ = 'FirestoreClient'
        mock_doc = MagicMock()
        mock_doc.get.side_effect = Exception("Firestore error")
        mock_db.collection.return_value.document.return_value = mock_doc
        
        builder = EnvironBuilder(method='GET', query_string={'code': 'test_auth_code', 'state': 'qbo_auth_state_xyz123'})
        env = builder.get_environ()
        request = Request(env)
        request.cookies = {'qbo_oauth_state': 'qbo_auth_state_xyz123'}

        with patch('requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                'access_token': 'test_access_token',
                'refresh_token': 'test_refresh_token',
                'expires_in': 3600
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            response, status_code = main.qbo_callback(request)
            assert status_code == 200
            assert response['status'] == 'success'

