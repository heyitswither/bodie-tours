import pytest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, ".")
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


sys.modules["functions_framework"] = DummyFunctionsFramework

mock_firestore = MagicMock()
mock_firestore.transactional = lambda f: f
sys.modules["google.cloud.firestore"] = mock_firestore
sys.modules["google.cloud"] = MagicMock(firestore=mock_firestore)

import main


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ,
        {
            "QBO_CLIENT_ID": "test_client_id",
            "QBO_CLIENT_SECRET": "test_client_secret",
            "QBO_REDIRECT_URI": "https://example.com/callback",
            "QBO_ENVIRONMENT": "sandbox",
        },
    ):
        yield


def test_qbo_login(mock_env):
    builder = EnvironBuilder(method="GET")
    env = builder.get_environ()
    request = Request(env)

    response = main.qbo_login(request)

    assert response.status_code == 302
    assert response.location.startswith("https://appcenter.intuit.com/connect/oauth2?")
    assert "client_id=test_client_id" in response.location
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcallback" in response.location
    assert "response_type=code" in response.location
    assert "scope=com.intuit.quickbooks.accounting" in response.location
    assert "state=" in response.location

    cookies = response.headers.getlist("Set-Cookie")
    assert any("qbo_oauth_state=" in cookie for cookie in cookies)


@patch("main.db")
@patch("requests.post")
def test_qbo_callback_success(mock_post, mock_db, mock_env):
    builder = EnvironBuilder(
        method="GET",
        query_string={
            "code": "test_auth_code",
            "realmId": "123456",
            "state": "qbo_auth_state_xyz123",
        },
    )
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {"qbo_oauth_state": "qbo_auth_state_xyz123"}

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "expires_in": 3600,
    }
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    mock_doc = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc

    response, status_code = main.qbo_callback(request)

    assert status_code == 200
    assert response["status"] == "success"

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    assert "Authorization" in kwargs["headers"]
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "test_auth_code"

    mock_db.collection.assert_called_with("config")
    mock_db.collection.return_value.document.assert_called_with("qbo_auth")
    mock_doc.set.assert_called_once()
    set_args = mock_doc.set.call_args[0][0]
    assert set_args["access_token"] == "test_access_token"
    assert set_args["refresh_token"] == "test_refresh_token"
    assert set_args["realmId"] == "123456"


def test_qbo_callback_missing_code(mock_env):
    builder = EnvironBuilder(method="GET", query_string={"state": "valid_state"})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {"qbo_oauth_state": "valid_state"}

    response, status_code = main.qbo_callback(request)

    assert status_code == 400
    assert response["status"] == "error"


@patch("requests.post")
def test_qbo_callback_request_exception(mock_post, mock_env):
    builder = EnvironBuilder(
        method="GET", query_string={"code": "test_auth_code", "state": "valid_state"}
    )
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {"qbo_oauth_state": "valid_state"}

    mock_post.side_effect = requests.exceptions.RequestException("Test error")

    response, status_code = main.qbo_callback(request)

    assert status_code == 500
    assert response["status"] == "error"
    assert "Failed to exchange token" in response["message"]


def test_qbo_callback_invalid_state(mock_env):
    # Test missing state in args
    builder = EnvironBuilder(method="GET", query_string={"code": "test_code"})
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {"qbo_oauth_state": "some_state"}
    response, status = main.qbo_callback(request)
    assert status == 400
    assert response["message"] == "Invalid state parameter"

    # Test missing state in cookies
    builder = EnvironBuilder(
        method="GET", query_string={"code": "test_code", "state": "some_state"}
    )
    env = builder.get_environ()
    request = Request(env)
    response, status = main.qbo_callback(request)
    assert status == 400
    assert response["message"] == "Invalid state parameter"

    # Test mismatching state
    builder = EnvironBuilder(
        method="GET", query_string={"code": "test_code", "state": "state1"}
    )
    env = builder.get_environ()
    request = Request(env)
    request.cookies = {"qbo_oauth_state": "state2"}
    response, status = main.qbo_callback(request)
    assert status == 400
    assert response["message"] == "Invalid state parameter"


def test_qbo_login_db_exception(mock_env):
    with patch("main.db") as mock_db:
        mock_db.__class__.__name__ = "FirestoreClient"
        mock_doc = MagicMock()
        mock_doc.get.side_effect = Exception("Firestore error")
        mock_db.collection.return_value.document.return_value = mock_doc

        builder = EnvironBuilder(method="GET")
        env = builder.get_environ()
        request = Request(env)

        response = main.qbo_login(request)
        assert response.status_code == 302
        assert "client_id=test_client_id" in response.location


def test_qbo_callback_db_exception(mock_env):
    with patch("main.db") as mock_db:
        mock_db.__class__.__name__ = "FirestoreClient"
        mock_doc = MagicMock()
        mock_doc.get.side_effect = Exception("Firestore error")
        mock_db.collection.return_value.document.return_value = mock_doc

        builder = EnvironBuilder(
            method="GET",
            query_string={"code": "test_auth_code", "state": "qbo_auth_state_xyz123"},
        )
        env = builder.get_environ()
        request = Request(env)
        request.cookies = {"qbo_oauth_state": "qbo_auth_state_xyz123"}

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
                "expires_in": 3600,
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            response, status_code = main.qbo_callback(request)
            assert status_code == 200
            assert response["status"] == "success"


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
def test_resolve_or_create_qbo_customer_existing(mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "QueryResponse": {
            "Customer": [{"Id": "cust_123"}]
        }
    }
    mock_get.return_value = mock_response

    guest_data = {
        "name": "Alice Smith",
        "email": "alice@example.com",
        "phone": "555-0199"
    }
    
    res = main.resolve_or_create_qbo_customer("real_token", "realm_123", guest_data)
    assert res == "cust_123"
    
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "query" in kwargs["params"]
    assert "alice@example.com" in kwargs["params"]["query"]


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
def test_resolve_or_create_qbo_customer_escaping(mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "QueryResponse": {
            "Customer": [{"Id": "cust_123"}]
        }
    }
    mock_get.return_value = mock_response

    guest_data = {
        "name": "O'Connor",
        "email": "o'connor@example.com",
        "phone": "123"
    }
    
    res = main.resolve_or_create_qbo_customer("real_token", "realm_123", guest_data)
    assert res == "cust_123"
    
    args, kwargs = mock_get.call_args
    query_str = kwargs["params"]["query"]
    assert "o\\'connor@example.com" in query_str


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
@patch("requests.post")
def test_resolve_or_create_qbo_customer_create_new(mock_post, mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "QueryResponse": {}
    }
    mock_get.return_value = mock_get_resp

    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 201
    mock_post_resp.json.return_value = {
        "Customer": {"Id": "new_cust_456"}
    }
    mock_post.return_value = mock_post_resp

    guest_data = {
        "name": "Bob Jones",
        "email": "bob@example.com",
        "phone": "555-0200"
    }
    
    res = main.resolve_or_create_qbo_customer("real_token", "realm_123", guest_data)
    assert res == "new_cust_456"
    
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["DisplayName"] == "Bob Jones"
    assert kwargs["json"]["PrimaryEmailAddr"]["Address"] == "bob@example.com"


def test_resolve_or_create_qbo_customer_missing_guest_data():
    res = main.resolve_or_create_qbo_customer("token", "realm", None)
    assert res == "1"
    
    res = main.resolve_or_create_qbo_customer("token", "realm", "not_a_dict")
    assert res == "1"


def test_resolve_or_create_qbo_customer_missing_email():
    res = main.resolve_or_create_qbo_customer("token", "realm", {"name": "Bob"})
    assert res == "1"
    
    res = main.resolve_or_create_qbo_customer("token", "realm", {"name": "Bob", "email": None})
    assert res == "1"


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
def test_resolve_or_create_qbo_customer_query_exception(mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    mock_get.side_effect = Exception("Network timeout")
    
    guest_data = {"name": "Bob", "email": "bob@example.com"}
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data)
    assert res == "1"


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
@patch("requests.post")
def test_resolve_or_create_qbo_customer_create_failure(mock_post, mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {"QueryResponse": {}}
    mock_get.return_value = mock_get_resp

    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 400
    mock_post_resp.text = "Bad Request"
    mock_post.return_value = mock_post_resp

    guest_data = {"name": "Bob", "email": "bob@example.com"}
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data)
    assert res == "1"


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
def test_resolve_or_create_qbo_customer_firestore_config(mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {"environment": "production"}
    mock_db.collection.return_value.document.return_value.get.return_value = mock_doc
    
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "QueryResponse": {"Customer": [{"Id": "cust_prod"}]}
    }
    mock_get.return_value = mock_get_resp

    guest_data = {"name": "Bob", "email": "bob@example.com"}
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data)
    assert res == "cust_prod"
    
    args, _ = mock_get.call_args
    assert "https://quickbooks.api.intuit.com" in args[0]


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
def test_resolve_or_create_qbo_customer_firestore_exception(mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"
    mock_db.collection.side_effect = Exception("Firestore fails")
    
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        "QueryResponse": {"Customer": [{"Id": "cust_fallback"}]}
    }
    mock_get.return_value = mock_get_resp

    guest_data = {"name": "Bob", "email": "bob@example.com"}
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data)
    assert res == "cust_fallback"


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
@patch("requests.post")
def test_resolve_or_create_qbo_customer_duplicate_name_resolved_by_query(mock_post, mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"

    # 1. Initial query by email returns no customer
    mock_get_email_resp = MagicMock()
    mock_get_email_resp.status_code = 200
    mock_get_email_resp.json.return_value = {"QueryResponse": {}}

    # 2. Query by DisplayName returns existing customer ID "resolved_cust_999"
    mock_get_name_resp = MagicMock()
    mock_get_name_resp.status_code = 200
    mock_get_name_resp.json.return_value = {
        "QueryResponse": {
            "Customer": [{"Id": "resolved_cust_999"}]
        }
    }

    # Set mock_get side effect to handle both requests in order
    mock_get.side_effect = [mock_get_email_resp, mock_get_name_resp]

    # Create returns 400 with duplicate name code 6240
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 400
    mock_post_resp.text = '{"Fault":{"Error":[{"Message":"Name collision","code":"6240"}]}}'
    mock_post.return_value = mock_post_resp

    guest_data = {"name": "Duplicate Bob", "email": "bob@example.com"}
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data)

    assert res == "resolved_cust_999"
    assert mock_get.call_count == 2
    assert mock_post.call_count == 1

    # Verify query strings
    get_calls = mock_get.call_args_list
    # First get query should check PrimaryEmailAddr
    assert "PrimaryEmailAddr =" in get_calls[0][1]["params"]["query"]
    # Second get query should check DisplayName
    assert "DisplayName = 'Duplicate Bob'" in get_calls[1][1]["params"]["query"]


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
@patch("requests.post")
def test_resolve_or_create_qbo_customer_duplicate_name_vendor_collision_creates_unique_suffix(mock_post, mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"

    # 1. Initial query by email returns no customer
    mock_get_email_resp = MagicMock()
    mock_get_email_resp.status_code = 200
    mock_get_email_resp.json.return_value = {"QueryResponse": {}}

    # 2. Query by DisplayName returns no Customer (collision is with Vendor/Employee)
    mock_get_name_resp = MagicMock()
    mock_get_name_resp.status_code = 200
    mock_get_name_resp.json.return_value = {"QueryResponse": {}}

    mock_get.side_effect = [mock_get_email_resp, mock_get_name_resp]

    # 1. Initial create POST returns 400 with duplicate name code 6240
    mock_post_fail_resp = MagicMock()
    mock_post_fail_resp.status_code = 400
    mock_post_fail_resp.text = '{"Fault":{"Error":[{"Message":"Name collision","code":"6240"}]}}'

    # 2. Retry create POST with unique suffix succeeds
    mock_post_success_resp = MagicMock()
    mock_post_success_resp.status_code = 201
    mock_post_success_resp.json.return_value = {
        "Customer": {"Id": "unique_suffix_cust_777"}
    }

    mock_post.side_effect = [mock_post_fail_resp, mock_post_success_resp]

    guest_data = {"name": "Vendor Bob", "email": "bob@example.com"}
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data)

    assert res == "unique_suffix_cust_777"
    assert mock_get.call_count == 2
    assert mock_post.call_count == 2

    # Verify the unique display name retry payload
    post_calls = mock_post.call_args_list
    assert post_calls[0][1]["json"]["DisplayName"] == "Vendor Bob"
    assert post_calls[1][1]["json"]["DisplayName"] == "Vendor Bob - bob@example.com"


@patch.dict(os.environ, {"TEST_QBO_CUSTOMER_LOGIC": "1"})
@patch("main.db")
@patch("requests.get")
@patch("requests.post")
def test_resolve_or_create_qbo_customer_idempotency_requestid(mock_post, mock_get, mock_db):
    mock_db.__class__.__name__ = "FirestoreClient"

    # Query returns no existing customer
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {"QueryResponse": {}}
    mock_get.return_value = mock_get_resp

    # Create POST returns 201
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 201
    mock_post_resp.json.return_value = {"Customer": {"Id": "cust_idempotent_123"}}
    mock_post.return_value = mock_post_resp

    guest_data = {"name": "Idempotent Guest", "email": "idempotent@example.com"}
    booking_id = "test_booking_id_999"
    res = main.resolve_or_create_qbo_customer("token", "realm", guest_data, booking_id=booking_id)

    assert res == "cust_idempotent_123"
    assert mock_post.call_count == 1
    
    # Verify requestid parameter is appended deterministically
    import uuid
    expected_token = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bodie-tours-customer-{booking_id}"))
    called_url = mock_post.call_args[0][0]
    assert f"requestid={expected_token}" in called_url
