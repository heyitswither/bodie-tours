import pytest


def test_app_routes(client):
    """Verify all required routes are registered in the Flask app."""
    routes = [str(p) for p in client.application.url_map.iter_rules()]
    assert "/booking" in routes
    assert "/qbo/login" in routes
    assert "/qbo/callback" in routes
    assert "/prune" in routes
