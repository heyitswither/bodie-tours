import pytest
from dev_server import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_index_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "/handle-booking" in html
    assert "/firestore" in html
    assert "false" in html  # replaced hostname check


def test_firestore_public_endpoint(client):
    response = client.get("/firestore/public?pageSize=50")
    assert response.status_code == 200
    data = response.get_json()
    assert "documents" in data
    docs = data["documents"]
    assert len(docs) >= 1
    doc = docs[0]
    assert doc["name"].endswith("2026-06-15")
    slots = doc["fields"]["slots"]["mapValue"]["fields"]
    assert slots["10:00"]["mapValue"]["fields"]["status"]["stringValue"] == "AVAILABLE"
    assert slots["13:00"]["mapValue"]["fields"]["status"]["stringValue"] == "SOLD_OUT"
    assert slots["16:00"]["mapValue"]["fields"]["status"]["stringValue"] == "AVAILABLE"


def test_handle_booking_success(client):
    payload = {
        "date": "2026-06-15",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Jane Doe", "email": "jane@example.com", "phone": "555-1234"},
    }
    response = client.post("/handle-booking", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert "booking_id" in data
    assert "payment_link" in data


def test_handle_booking_conflict(client):
    payload = {
        "date": "2026-06-15",
        "time": "10:00",
        "party_size": 2,
        "guest": {
            "name": "Conflict Error",
            "email": "jane@example.com",
            "phone": "555-1234",
        },
    }
    response = client.post("/handle-booking", json=payload)
    assert response.status_code == 409
    data = response.get_json()
    assert "conflict" in data["message"].lower()


def test_handle_booking_server_error(client):
    payload = {
        "date": "2026-06-15",
        "time": "10:00",
        "party_size": 2,
        "guest": {
            "name": "Server Error",
            "email": "jane@example.com",
            "phone": "555-1234",
        },
    }
    response = client.post("/handle-booking", json=payload)
    assert response.status_code == 500
    data = response.get_json()
    assert "internal server error" in data["message"].lower()
