import pytest
from unittest.mock import patch, MagicMock
from flask import Flask, request
import sys


# ---- Pre-mock google.cloud and functions_framework before any import of main ----
class _DummyFF:
    @staticmethod
    def http(func):
        return func


if "functions_framework" not in sys.modules:
    sys.modules["functions_framework"] = _DummyFF

_mock_fs = MagicMock()
_mock_fs.transactional = lambda f: f
_mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
_mock_fs.FieldFilter = MagicMock

if "google.cloud.firestore" not in sys.modules:
    _gc_mock = MagicMock()
    _gc_mock.firestore = _mock_fs
    sys.modules["google.cloud"] = _gc_mock
    sys.modules["google.cloud.firestore"] = _mock_fs

sys.path.insert(0, ".")
with patch("google.cloud.firestore.Client", MagicMock()):
    import main
    import prune_unpaid_slots


def _wrap(app_obj, func):
    def wrapped():
        result = func(request)
        if isinstance(result, tuple):
            if len(result) == 3:
                body, status, headers = result
                return app_obj.response_class(
                    response=(
                        app_obj.json.dumps(body)
                        if isinstance(body, dict)
                        else str(body)
                    ),
                    status=status,
                    headers=headers,
                )
            elif len(result) == 2:
                body, status = result
                return app_obj.response_class(
                    response=(
                        app_obj.json.dumps(body)
                        if isinstance(body, dict)
                        else str(body)
                    ),
                    status=status,
                )
        return result

    return wrapped


@pytest.fixture
def app():
    app = Flask(__name__)
    app.add_url_rule(
        "/booking",
        "handle_booking",
        _wrap(app, main.handle_booking),
        methods=["POST", "OPTIONS", "GET"],
    )
    app.add_url_rule(
        "/qbo/login", "qbo_login", _wrap(app, main.qbo_login), methods=["GET"]
    )
    app.add_url_rule(
        "/qbo/callback", "qbo_callback", _wrap(app, main.qbo_callback), methods=["GET"]
    )
    app.add_url_rule(
        "/qbo/webhook", "qbo_webhook", _wrap(app, main.qbo_webhook), methods=["POST"]
    )
    app.add_url_rule(
        "/prune",
        "prune",
        _wrap(app, prune_unpaid_slots.prune_unpaid_slots),
        methods=["POST", "GET"],
    )
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    """Ensure required env vars are set for QBO token refresh."""
    monkeypatch.setenv("QBO_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("QBO_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("QBO_REDIRECT_URI", "https://example.com/callback")
    monkeypatch.setenv("QBO_ENVIRONMENT", "sandbox")


@pytest.fixture(autouse=True)
def mock_firestore():
    with patch("google.cloud.firestore.Client") as mock_client:
        yield mock_client


@pytest.fixture(autouse=True)
def mock_main_db(mock_firestore):
    """
    Tier4: Delegates to whatever mock_firestore.return_value is set to.
    patch_firestore in test_scenarios.py sets mock_firestore.return_value = MockFirestore().
    Since patch_firestore has autouse=True and is in the test module, it runs after
    conftest fixtures. We use a context manager that captures the live value at patch time.
    """

    # We patch main.db LAZILY — after all other autouse fixtures have run —
    # by using a proxy object that forwards all attribute access to the current value.
    class _DBProxy:
        """Forwards all attribute access to mock_firestore.return_value at call time."""

        def __getattr__(self, name):
            return getattr(mock_firestore.return_value, name)

        def __call__(self, *args, **kwargs):
            return mock_firestore.return_value(*args, **kwargs)

    proxy = _DBProxy()
    with patch("main.db", proxy), patch("prune_unpaid_slots.db", proxy):
        yield proxy


# Import shared URL-aware mock_requests_post and mock_requests_get
from tests.e2e._conftest_helpers import mock_requests_post, mock_requests_get
