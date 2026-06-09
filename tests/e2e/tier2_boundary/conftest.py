import pytest
from unittest.mock import patch, MagicMock
from flask import Flask, request, jsonify
import json

app = Flask(__name__)

@app.route('/qbo_oauth', methods=['GET', 'POST'])
def qbo_oauth(): return jsonify({}), 200

@app.route('/qbo_invoice', methods=['POST'])
def qbo_invoice(): return jsonify({}), 200

@app.route('/m365_availability', methods=['GET'])
def m365_availability(): return jsonify({}), 200

@app.route('/m365_event_injection', methods=['POST'])
def m365_event_injection(): return jsonify({}), 200

@app.route('/pruning_ttl_calc', methods=['POST'])
def pruning_ttl_calc(): return jsonify({}), 200

@app.route('/pruning_reminder', methods=['POST'])
def pruning_reminder(): return jsonify({}), 200

@app.route('/pruning_cancellation', methods=['POST'])
def pruning_cancellation(): return jsonify({}), 200

@app.route('/pruning_event_removal', methods=['POST'])
def pruning_event_removal(): return jsonify({}), 200

@app.route('/pruning_cleanup', methods=['POST'])
def pruning_cleanup(): return jsonify({}), 200

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

@pytest.fixture(autouse=True)
def mock_firestore():
    with patch('google.cloud.firestore.Client') as mock_client:
        yield mock_client

@pytest.fixture(autouse=True)
def mock_requests():
    with patch('requests.post') as mock_post, patch('requests.get') as mock_get:
        yield mock_post, mock_get
