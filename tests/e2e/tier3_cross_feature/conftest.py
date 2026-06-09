import pytest
from unittest.mock import patch, MagicMock
from flask import Flask, request
import sys

# ---- Pre-mock google.cloud and functions_framework before any import of main ----
class _DummyFF:
    @staticmethod
    def http(func):
        return func

if 'functions_framework' not in sys.modules:
    sys.modules['functions_framework'] = _DummyFF

_mock_fs = MagicMock()
_mock_fs.transactional = lambda f: f
_mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
_mock_fs.FieldFilter = MagicMock

if 'google.cloud.firestore' not in sys.modules:
    _gc_mock = MagicMock()
    _gc_mock.firestore = _mock_fs
    sys.modules['google.cloud'] = _gc_mock
    sys.modules['google.cloud.firestore'] = _mock_fs

sys.path.insert(0, '.')
with patch('google.cloud.firestore.Client', MagicMock()):
    import main
    import prune_unpaid_slots


def _wrap(app_obj, func):
    def wrapped():
        result = func(request)
        if isinstance(result, tuple):
            if len(result) == 3:
                body, status, headers = result
                return app_obj.response_class(
                    response=app_obj.json.dumps(body) if isinstance(body, dict) else str(body),
                    status=status, headers=headers
                )
            elif len(result) == 2:
                body, status = result
                return app_obj.response_class(
                    response=app_obj.json.dumps(body) if isinstance(body, dict) else str(body),
                    status=status
                )
        return result
    return wrapped


@pytest.fixture
def app():
    app = Flask(__name__)
    app.add_url_rule('/booking', 'handle_booking', _wrap(app, main.handle_booking), methods=['POST', 'OPTIONS'])
    app.add_url_rule('/qbo/login', 'qbo_login', _wrap(app, main.qbo_login), methods=['GET'])
    app.add_url_rule('/qbo/callback', 'qbo_callback', _wrap(app, main.qbo_callback), methods=['GET'])
    app.add_url_rule('/qbo/webhook', 'qbo_webhook', _wrap(app, main.qbo_webhook), methods=['POST'])
    app.add_url_rule('/prune', 'prune', _wrap(app, prune_unpaid_slots.prune_unpaid_slots), methods=['POST', 'GET'])
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def mock_firestore():
    with patch('google.cloud.firestore.Client') as mock_client:
        yield mock_client


# Import shared fixtures (mock_main_db, mock_requests_post, mock_requests_get)
from tests.e2e._conftest_helpers import mock_main_db, mock_requests_post, mock_requests_get

