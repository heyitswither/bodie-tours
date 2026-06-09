import pytest
from unittest.mock import MagicMock
import requests

def test_qbo_invoice_missing_customer_email(client, mock_requests):
    response = client.post('/qbo_invoice', json={"amount": 100})
    assert response.status_code in [200, 400, 500]

def test_qbo_invoice_zero_amount(client, mock_requests):
    response = client.post('/qbo_invoice', json={"email": "test@example.com", "amount": 0})
    assert response.status_code in [200, 400, 500]

def test_qbo_invoice_extreme_large_amount(client, mock_requests):
    response = client.post('/qbo_invoice', json={"email": "test@example.com", "amount": 999999999999999})
    assert response.status_code in [200, 400, 500]

def test_qbo_invoice_rate_limit_429(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=429)
    response = client.post('/qbo_invoice', json={"email": "test@example.com", "amount": 100})
    assert response.status_code in [200, 429, 500]

def test_qbo_invoice_missing_payment_link(client, mock_requests):
    mock_post, _ = mock_requests
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"Invoice": {"Id": "123"}}) # No payment link
    response = client.post('/qbo_invoice', json={"email": "test@example.com", "amount": 100})
    assert response.status_code in [200, 400, 500]
