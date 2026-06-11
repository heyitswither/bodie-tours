import os
import pytest
from unittest import mock
import requests

from requests_retry import _retry_request

def test_requests_retry_success():
    mock_original_request = mock.MagicMock()
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_original_request.return_value = mock_resp

    with mock.patch("requests_retry._original_request", mock_original_request):
        resp = _retry_request("GET", "https://api.example.com/test", timeout=10)
        assert resp.status_code == 200
        mock_original_request.assert_called_once_with("GET", "https://api.example.com/test", timeout=10)

def test_requests_retry_failure_then_success():
    mock_original_request = mock.MagicMock()
    mock_resp_fail = mock.MagicMock()
    mock_resp_fail.status_code = 502
    mock_resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError("Bad Gateway", response=mock_resp_fail)
    
    mock_resp_success = mock.MagicMock()
    mock_resp_success.status_code = 200
    
    mock_original_request.side_effect = [mock_resp_fail, mock_resp_success]

    # Temporarily remove PYTEST_CURRENT_TEST to trigger retry path
    original_env = os.environ.get("PYTEST_CURRENT_TEST")
    if original_env:
        del os.environ["PYTEST_CURRENT_TEST"]
        
    try:
        with mock.patch("requests_retry._original_request", mock_original_request):
            resp = _retry_request("POST", "https://api.example.com/submit", timeout=5)
            assert resp.status_code == 200
            assert mock_original_request.call_count == 2
            
            calls = mock_original_request.call_args_list
            assert calls[0][0] == ("POST", "https://api.example.com/submit")
            assert calls[0][1]["timeout"] == 5
            
            assert calls[1][0] == ("POST", "https://api.example.com/submit")
            assert calls[1][1]["timeout"] == 25  # max(25, 5*2)
    finally:
        if original_env:
            os.environ["PYTEST_CURRENT_TEST"] = original_env

def test_requests_retry_both_failure():
    mock_original_request = mock.MagicMock()
    mock_resp_fail1 = mock.MagicMock()
    mock_resp_fail1.status_code = 500
    mock_resp_fail1.raise_for_status.side_effect = requests.exceptions.HTTPError("Internal Server Error", response=mock_resp_fail1)
    
    mock_resp_fail2 = mock.MagicMock()
    mock_resp_fail2.status_code = 503
    mock_original_request.side_effect = [mock_resp_fail1, mock_resp_fail2]

    original_env = os.environ.get("PYTEST_CURRENT_TEST")
    if original_env:
        del os.environ["PYTEST_CURRENT_TEST"]
        
    try:
        with mock.patch("requests_retry._original_request", mock_original_request):
            resp = _retry_request("GET", "https://api.example.com/data", timeout=15)
            assert resp.status_code == 503
            assert mock_original_request.call_count == 2
            
            calls = mock_original_request.call_args_list
            assert calls[0][1]["timeout"] == 15
            assert calls[1][1]["timeout"] == 30  # max(25, 15*2)
    finally:
        if original_env:
            os.environ["PYTEST_CURRENT_TEST"] = original_env
