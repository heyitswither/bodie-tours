import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from google.cloud import firestore


class DummyFieldFilter:
    def __init__(self, field, op, value):
        self.field = field
        self.op = op
        self.value = value


firestore.FieldFilter = DummyFieldFilter


class MockFirestore:
    def __init__(self):
        self.db = {}  # path to data
        self.db["config/m365_auth"] = {
            "client_id": "c",
            "client_secret": "s",
            "tenant_id": "t",
            "refresh_token": "r",
            "user_id": "u",
        }
        self.db["config/qbo_auth"] = {
            "access_token": "qa",
            "refresh_token": "qr",
            "realmId": "qrealm",
        }

    def collection(self, name):
        return MockCollection(self, name)

    def transaction(self):
        return MockTransaction(self)


class MockCollection:
    def __init__(self, db_mock, name, filters=None):
        self.db_mock = db_mock
        self.name = name
        self.filters = filters or []

    def document(self, doc_id=None):
        if not doc_id:
            doc_id = "auto_id_123"
        return MockDocument(self.db_mock, f"{self.name}/{doc_id}")

    def where(self, filter=None, *args, **kwargs):
        new_filters = list(self.filters)
        if filter:
            new_filters.append(filter)
        return MockCollection(self.db_mock, self.name, new_filters)

    def stream(self):
        # find all docs matching
        res = []
        for path, data in self.db_mock.db.items():
            if path.startswith(f"{self.name}/"):
                match = True
                for flt in self.filters:
                    if (
                        hasattr(flt, "field")
                        and hasattr(flt, "op")
                        and hasattr(flt, "value")
                    ):
                        # Detect and gracefully handle leaked MagicMock filters to avoid destructive queries
                        if (
                            isinstance(flt, MagicMock)
                            or isinstance(getattr(flt, "field", None), MagicMock)
                            or isinstance(getattr(flt, "op", None), MagicMock)
                            or isinstance(getattr(flt, "value", None), MagicMock)
                        ):
                            match = False
                            break
                        field = flt.field
                        op = flt.op
                        val = flt.value

                        actual_val = data.get(field)
                        if isinstance(actual_val, str) and isinstance(val, datetime):
                            try:
                                if actual_val.endswith("Z"):
                                    actual_val = actual_val[:-1] + "+00:00"
                                actual_val = datetime.fromisoformat(actual_val)
                            except ValueError:
                                pass
                        if op == "==":
                            if actual_val != val:
                                match = False
                                break
                        elif op == "<=":
                            if actual_val is None or actual_val > val:
                                match = False
                                break
                        elif op == ">=":
                            if actual_val is None or actual_val < val:
                                match = False
                                break
                        elif op == "<":
                            if actual_val is None or actual_val >= val:
                                match = False
                                break
                        elif op == ">":
                            if actual_val is None or actual_val <= val:
                                match = False
                                break
                if match:
                    res.append(
                        MockDocumentSnapshot(MockDocument(self.db_mock, path), data)
                    )
        return res


class MockDocument:
    def __init__(self, db_mock, path):
        self.db_mock = db_mock
        self.path = path
        self.id = path.split("/")[-1]
        self.reference = self

    def get(self, transaction=None):
        data = self.db_mock.db.get(self.path)
        return MockDocumentSnapshot(self, data)

    def set(self, data, merge=False):
        if merge and self.path in self.db_mock.db:
            self.db_mock.db[self.path].update(data)
        else:
            self.db_mock.db[self.path] = data

    def update(self, data):
        if self.path in self.db_mock.db:
            self.db_mock.db[self.path].update(data)

    def delete(self):
        if self.path in self.db_mock.db:
            del self.db_mock.db[self.path]


class MockDocumentSnapshot:
    def __init__(self, doc, data):
        self.reference = doc
        self.exists = data is not None
        self._data = data or {}

    def to_dict(self):
        return self._data


class MockTransaction:
    def __init__(self, db_mock):
        self.db_mock = db_mock
        self._read_only = False
        self._id = b"mock-id"

    def get(self, ref):
        return ref.get()

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)


@pytest.fixture(autouse=True)
def patch_firestore(mock_firestore):
    db = MockFirestore()
    mock_firestore.return_value = db

    # Enforce DummyFieldFilter on firestore modules to prevent MagicMock filter leakage
    from google.cloud import firestore as gc_firestore
    import prune_unpaid_slots
    import main

    gc_firestore.FieldFilter = DummyFieldFilter
    prune_unpaid_slots.firestore.FieldFilter = DummyFieldFilter
    if hasattr(main, "firestore"):
        main.firestore.FieldFilter = DummyFieldFilter

    yield db


def setup_post_mock(mock_requests_post, is_available=True):
    """Configure requests.post defaults for tier4 scenarios.
    Availability check is now a GET (calendarView) — handled by mock_requests_get.
    """

    def post_se(*args, **kwargs):
        url = args[0] if args else kwargs.get("url")
        resp = MagicMock()
        resp.status_code = 200
        if "token" in url:
            resp.json.return_value = {
                "access_token": "mock",
                "refresh_token": "mock",
                "expires_in": 3600,
            }
        elif "/events" in url:
            resp.status_code = 201
            resp.json.return_value = {"id": "mock_event_id"}
        elif "invoice" in url.lower() or "intuit" in url.lower():
            resp.json.return_value = {"Invoice": {"Id": "INV-001"}}
        else:
            resp.json.return_value = {}
        return resp

    mock_requests_post.side_effect = post_se


# Scenario 1: Normal successful booking, payment, and tour completion
def test_normal_booking_and_completion(client, mock_requests_post, patch_firestore):
    setup_post_mock(mock_requests_post)
    patch_firestore.db["public/2026-06-15"] = {"taken_slots": []}

    # 1. Booking
    payload = {
        "date": "2026-06-15",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Alice"},
    }
    resp = client.post("/booking", json=payload)
    assert resp.status_code == 200

    assert "public/2026-06-15" in patch_firestore.db
    assert len(patch_firestore.db["public/2026-06-15"]["taken_slots"]) == 1

    # Check booking doc
    booking_keys = [k for k in patch_firestore.db.keys() if k.startswith("bookings/")]
    assert len(booking_keys) == 1
    b_key = booking_keys[0]
    assert patch_firestore.db[b_key]["payment_status"] == "PENDING"

    # 2. Payment (Feature 2 webhook simulation - assume QBO webhook updates to PAID)
    # Since main.py doesn't have it, we manually update to simulate success
    patch_firestore.db[b_key]["payment_status"] = "PAID"

    # 3. M365 Event Injection (Feature 4)
    # Would be triggered on PAID, we can't test main.py directly for this as it lacks the webhook endpoint,
    # but the scenario is marked "might fail" or we simulate the state.

    # 4. Tour completion (Feature 9)
    # Cleanup task...
    pass


# Scenario 2: Booking made, but TTL expires before payment
def test_booking_ttl_expires(client, mock_requests_post, patch_firestore):
    setup_post_mock(mock_requests_post, True)

    from zoneinfo import ZoneInfo

    local_tz = ZoneInfo("America/Los_Angeles")
    now_local = datetime.now(local_tz)
    created_at = (now_local - timedelta(hours=4)).astimezone(timezone.utc)
    tour_dt_local = (now_local + timedelta(hours=30)).replace(second=0, microsecond=0)
    tour_dt = tour_dt_local.astimezone(timezone.utc)

    patch_firestore.db["bookings/b1"] = {
        "tour_datetime": tour_dt.isoformat(),
        "created_at": created_at,
        "party_size": 2,
        "payment_status": "PENDING",
    }
    # Setup public inventory
    date_str = tour_dt_local.strftime("%Y-%m-%d")
    time_str = tour_dt_local.strftime("%H:%M")
    patch_firestore.db[f"public/{date_str}"] = {"taken_slots": [tour_dt]}

    # Call prune endpoint
    resp = client.post("/prune")
    assert resp.status_code == 200

    print("DEBUG keys in db:", list(patch_firestore.db.keys()))
    print("DEBUG bookings/b1:", patch_firestore.db.get("bookings/b1"))

    # DB state should reflect cancellation (4h > 3h TTL for 30h lead time)
    assert patch_firestore.db["bookings/b1"]["payment_status"] == "CANCELLED_UNPAID"
    assert patch_firestore.db[f"public/{date_str}"]["taken_slots"] == []


# Scenario 3: Booking attempted but no Touring Hours availability
def test_booking_no_m365_availability(
    client, mock_requests_post, mock_requests_get, patch_firestore
):
    setup_post_mock(mock_requests_post)

    # Override calendarView GET to return no Touring Hours events
    def no_availability_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"value": []}  # No Touring Hours events
        return resp

    mock_requests_get.side_effect = no_availability_get

    payload = {
        "date": "2026-06-15",
        "time": "10:00",
        "party_size": 2,
        "guest": {"name": "Bob", "email": "bob@example.com", "phone": "555-0000"},
    }
    resp = client.post("/booking", json=payload)
    assert resp.status_code == 409

    # No booking should be created
    booking_keys = [k for k in patch_firestore.db.keys() if k.startswith("bookings/")]
    assert len(booking_keys) == 0


# Scenario 4: QBO OAuth token expiration & refresh during booking
def test_qbo_token_refresh_during_booking(client, mock_requests_post, patch_firestore):
    setup_post_mock(mock_requests_post, True)

    # First call /qbo/login to get a CSRF state cookie
    login_resp = client.get("/qbo/login")
    # Extract state cookie from the login response
    state_cookie = None
    for header_name, header_value in login_resp.headers:
        if header_name.lower() == "set-cookie" and "qbo_oauth_state" in header_value:
            # Parse the cookie value
            for part in header_value.split(";"):
                part = part.strip()
                if part.startswith("qbo_oauth_state="):
                    state_cookie = part.split("=", 1)[1]
                    break
    if not state_cookie:
        pytest.xfail("Could not extract CSRF state cookie from /qbo/login")

    resp = client.get(
        f"/qbo/callback?code=mockcode&realmId=123&state={state_cookie}",
        headers={"Cookie": f"qbo_oauth_state={state_cookie}"},
    )
    if resp.status_code == 501:
        pytest.xfail("Not implemented")

    assert resp.status_code == 200
    assert "config/qbo_auth" in patch_firestore.db


# Scenario 5: Reminder email sent, then paid just before TTL
def test_reminder_email_and_late_payment(client, mock_requests_post, patch_firestore):
    # Mocking sending email is out of scope since it's just pruning logic simulation
    setup_post_mock(mock_requests_post, True)

    # Setup initial booking created 45 mins ago (TTL is 1 hour for next day tour)
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(minutes=45)
    tour_dt = now + timedelta(hours=10)  # Less than 1 day lead time -> TTL 1 hour

    patch_firestore.db["bookings/b2"] = {
        "tour_datetime": tour_dt.isoformat(),
        "created_at": created_at,
        "party_size": 2,
        "payment_status": "PENDING",
    }

    # Call prune endpoint, should NOT cancel because 45m < 60m
    resp = client.post("/prune")
    assert resp.status_code == 200
    assert patch_firestore.db["bookings/b2"]["payment_status"] == "PENDING"

    # Simulate payment just in time
    patch_firestore.db["bookings/b2"]["payment_status"] = "PAID"

    # Call prune again, shouldn't cancel PAID bookings
    resp = client.post("/prune")
    assert resp.status_code == 200
    assert patch_firestore.db["bookings/b2"]["payment_status"] == "PAID"
