import sys
import types
import pytest
from unittest.mock import MagicMock, patch
import requests

# Globally mock google.cloud.firestore for unit tests
mock_firestore_module = MagicMock()
mock_firestore_module.transactional = lambda f: f
mock_firestore_module.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
mock_firestore_module.FieldFilter = MagicMock

sys.modules["google.cloud.firestore"] = mock_firestore_module
if "google.cloud" in sys.modules:
    sys.modules["google.cloud"].firestore = mock_firestore_module
else:
    pkg = types.ModuleType("google.cloud")
    pkg.__path__ = []
    pkg.firestore = mock_firestore_module
    sys.modules["google.cloud"] = pkg


@pytest.fixture(autouse=True)
def mock_requests_request_fallback():
    """
    Globally intercept requests.request calls and delegate them to requests.get,
    requests.post, requests.delete, etc.
    This ensures that any test mocking requests.get or requests.post (either via 
    unittest.mock.patch or standard pytest fixtures) will work seamlessly when 
    the code under test calls requests.request.
    """
    def patched_request(method, url, **kwargs):
        method_upper = method.upper()
        # Check if requests.get, requests.post, or requests.delete is mocked
        is_get_mocked = hasattr(requests.get, "assert_called") or hasattr(requests.get, "return_value")
        is_post_mocked = hasattr(requests.post, "assert_called") or hasattr(requests.post, "return_value")
        is_delete_mocked = hasattr(requests.delete, "assert_called") or hasattr(requests.delete, "return_value")

        if method_upper == "GET" and is_get_mocked:
            return requests.get(url, **kwargs)
        elif method_upper == "POST" and is_post_mocked:
            return requests.post(url, **kwargs)
        elif method_upper == "DELETE" and is_delete_mocked:
            return requests.delete(url, **kwargs)
        
        # If not mocked, return a default mock response for safety in tests
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}
        resp.text = "{}"
        return resp

    with patch("requests.request", side_effect=patched_request):
        yield
