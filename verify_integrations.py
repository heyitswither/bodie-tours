#!/usr/bin/env python3
import sys
import os
import requests
import subprocess
import time
from datetime import datetime, timezone, timedelta
from google.cloud import firestore
import google.auth
import google.auth.transport.requests

# ANSI Escape Codes for color-coded output
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"


def print_pass(msg):
    print(f"{COLOR_GREEN}[PASS] {msg}{COLOR_RESET}")


def print_fail(msg):
    print(f"{COLOR_RED}[FAIL] {msg}{COLOR_RESET}", file=sys.stderr)


def print_warn(msg):
    print(f"{COLOR_YELLOW}[WARN] {msg}{COLOR_RESET}")


def print_info(msg):
    print(f"[INFO] {msg}")


def _resolve_qbo_credentials(auth_data):
    """
    Resolve client_id, client_secret, verifier_token, and redirect_uri from qbo_auth config.
    Matches the resolution logic in main.py.
    """
    if not isinstance(auth_data, dict):
        auth_data = {}

    env = auth_data.get("environment")
    if not isinstance(env, str):
        env = os.environ.get("QBO_ENVIRONMENT")
    if not isinstance(env, str):
        env = "sandbox"
    env = env.lower().strip()

    if env == "sandbox":
        client_id = auth_data.get("dev-id")
        client_secret = auth_data.get("dev-secret")
        verifier_token = auth_data.get("dev-verifier_token") or auth_data.get(
            "dev-verify"
        )
    else:
        client_id = auth_data.get("prod-id")
        client_secret = auth_data.get("prod-secret")
        verifier_token = auth_data.get("prod-verifier_token") or auth_data.get(
            "prod-verify"
        )

    if not isinstance(client_id, str):
        client_id = auth_data.get("client_id")
    if not isinstance(client_secret, str):
        client_secret = auth_data.get("client_secret")
    if not isinstance(verifier_token, str):
        verifier_token = auth_data.get("verifier_token")

    if not isinstance(client_id, str):
        client_id = os.environ.get("QBO_CLIENT_ID")
    if not isinstance(client_secret, str):
        client_secret = os.environ.get("QBO_CLIENT_SECRET")

    redirect_uri = auth_data.get("callback_url")
    if not isinstance(redirect_uri, str):
        redirect_uri = auth_data.get("redirect_uri")
    if not isinstance(redirect_uri, str):
        redirect_uri = os.environ.get("QBO_REDIRECT_URI")

    return client_id, client_secret, verifier_token, redirect_uri


def open_browser_and_poll(db, doc_path, login_url, timeout=300):
    print_warn(f"Authorization tokens missing in config/{doc_path}.")
    print_info(f"Opening web browser to authorize: {login_url}")
    try:
        # Only attempt to auto-open the browser when the BROWSER env var is set
        if os.environ.get("BROWSER"):
            subprocess.run(
                ["xdg-open", login_url],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            print_info(
                "Auto-opening browser disabled. Set the BROWSER env var to enable."
            )
            print_info(f"Please open this URL manually in your browser: {login_url}")
    except Exception as e:
        print_warn(f"Failed to automatically open browser via xdg-open: {e}")
        print_info(f"Please open this URL manually in your browser: {login_url}")

    print_info("Waiting and polling Firestore for token update (Ctrl+C to cancel)...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        time.sleep(5)
        doc = db.collection("config").document(doc_path).get()
        if doc.exists:
            data = doc.to_dict() or {}
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            if access_token and refresh_token:
                print_pass("Tokens updated successfully in Firestore!")
                return data
    raise TimeoutError("Timed out waiting for authorization token update.")


def test_firestore(db):
    print_info("Running Firestore Read/Write/Delete Test...")
    try:
        doc_ref = db.collection("config").document("verify_test_temp")
        test_data = {
            "test_field": "verification_value",
            "timestamp": firestore.SERVER_TIMESTAMP,
        }
        # Write
        doc_ref.set(test_data)

        # Read
        doc = doc_ref.get()
        if not doc.exists:
            raise Exception("Document verify_test_temp does not exist after write.")
        data = doc.to_dict() or {}
        if data.get("test_field") != "verification_value":
            raise Exception(f"Unexpected data in verify_test_temp: {data}")

        # Delete
        doc_ref.delete()

        # Verify deleted
        doc = doc_ref.get()
        if doc.exists:
            raise Exception("Document verify_test_temp still exists after delete.")

        print_pass("Firestore Read/Write/Delete Test succeeded.")
        return True
    except Exception as e:
        print_fail(f"Firestore Read/Write/Delete Test failed: {e}")
        return False


def test_qbo(db):
    print_info("Running QBO Token and Connection Test...")
    try:
        doc_ref = db.collection("config").document("qbo_auth")
        doc = doc_ref.get()
        if not doc.exists:
            print_warn("QBO config/qbo_auth document does not exist.")
            sys.exit(1)

        auth_data = doc.to_dict() or {}
        access_token = auth_data.get("access_token")
        refresh_token = auth_data.get("refresh_token")

        # Check empty/missing tokens, launch browser & poll if missing
        if not access_token or not refresh_token:
            auth_data = open_browser_and_poll(
                db,
                "qbo_auth",
                "https://us-west2-bodie-tours-prod.cloudfunctions.net/qbo-login",
            )
            access_token = auth_data.get("access_token")
            refresh_token = auth_data.get("refresh_token")

        realm_id = auth_data.get("realmId")
        expires_at = auth_data.get("expires_at")

        # Check expiration. If expired or close to expiration (expires in less than 5 minutes), refresh
        now = datetime.now(timezone.utc)
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = now - timedelta(hours=1)

        if now >= expires_at - timedelta(minutes=5):
            print_info("QBO token is expired or close to expiration. Refreshing...")
            client_id, client_secret, _, _ = _resolve_qbo_credentials(auth_data)

            if not all([client_id, client_secret]):
                raise Exception(
                    "Missing QBO client credentials (client_id/client_secret) to refresh token."
                )

            token_endpoint = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
            import base64

            auth_str = f"{client_id}:{client_secret}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()

            headers = {
                "Authorization": f"Basic {b64_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

            res = requests.post(token_endpoint, headers=headers, data=data, timeout=10)
            res.raise_for_status()
            token_data = res.json()

            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 3600)
            new_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )

            update_payload = {
                "access_token": new_access_token,
                "expires_at": new_expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if new_refresh_token:
                update_payload["refresh_token"] = new_refresh_token

            doc_ref.update(update_payload)
            print_info("QBO tokens updated in Firestore.")
            access_token = new_access_token

        if not realm_id:
            raise Exception("QBO realmId is missing in config.")

        print_info(f"Performing live sandbox QBO query for realm ID: {realm_id}")
        qbo_url = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{realm_id}/companyinfo/{realm_id}?minorversion=65"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        res = requests.get(qbo_url, headers=headers, timeout=10)
        res.raise_for_status()

        print_pass("QBO Token and Connection Test succeeded.")
        return True
    except SystemExit:
        raise
    except Exception as e:
        print_fail(f"QBO Token and Connection Test failed: {e}")
        return False


def test_m365(db):
    print_info("Running M365 Token and Connection Test...")
    try:
        doc_ref = db.collection("config").document("m365_auth")
        doc = doc_ref.get()
        if not doc.exists:
            print_warn("M365 config/m365_auth document does not exist.")
            sys.exit(1)

        m365_data = doc.to_dict() or {}
        access_token = m365_data.get("access_token")
        refresh_token = m365_data.get("refresh_token")

        # Check empty/missing tokens, launch browser & poll if missing
        if not access_token or not refresh_token:
            m365_data = open_browser_and_poll(
                db,
                "m365_auth",
                "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-login",
            )
            access_token = m365_data.get("access_token")
            refresh_token = m365_data.get("refresh_token")

        user_id = m365_data.get("user_id")
        calendar_id = m365_data.get("calendar_id")
        expires_at = m365_data.get("expires_at")

        # Check expiration. If expired or close to expiration (expires in less than 5 minutes), refresh
        now = datetime.now(timezone.utc)
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = now - timedelta(hours=1)

        if now >= expires_at - timedelta(minutes=5):
            print_info("M365 token is expired or close to expiration. Refreshing...")
            client_id = m365_data.get("client_id") or os.environ.get("M365_CLIENT_ID")
            client_secret = m365_data.get("client_secret") or os.environ.get(
                "M365_CLIENT_SECRET"
            )
            tenant_id = m365_data.get("tenant_id") or os.environ.get(
                "M365_TENANT_ID", "common"
            )

            if not all([client_id, client_secret]):
                raise Exception(
                    "Missing M365 client credentials (client_id/client_secret) to refresh token."
                )

            token_url = (
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            )
            payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }

            res = requests.post(token_url, data=payload, timeout=10)
            res.raise_for_status()
            token_response = res.json()

            new_access_token = token_response.get("access_token")
            new_refresh_token = token_response.get("refresh_token")
            expires_in = token_response.get("expires_in", 3600)
            new_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )

            update_data = {
                "access_token": new_access_token,
                "expires_at": new_expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if new_refresh_token:
                update_data["refresh_token"] = new_refresh_token

            doc_ref.update(update_data)
            print_info("M365 tokens updated in Firestore.")
            access_token = new_access_token

        if not user_id:
            raise Exception("M365 user_id is missing in config.")

        # 1. Query calendar availability
        print_info(f"Querying M365 calendar availability for user ID: {user_id}")
        now_dt = datetime.now(timezone.utc)
        seven_days_later = now_dt + timedelta(days=7)
        start_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = seven_days_later.strftime("%Y-%m-%dT%H:%M:%SZ")

        cal_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView?startDateTime={start_str}&endDateTime={end_str}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        res = requests.get(cal_url, headers=headers, timeout=10)
        res.raise_for_status()
        print_info("M365 calendar availability query succeeded.")

        # 2. Inject a temporary event
        print_info("Injecting a temporary M365 event...")
        event_start = datetime.now(timezone.utc) + timedelta(hours=1)
        event_end = event_start + timedelta(hours=1)

        event_payload = {
            "subject": "Verify Integrations Test Run",
            "body": {
                "contentType": "HTML",
                "content": "Temporary test event for integration verification.",
            },
            "start": {
                "dateTime": event_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": event_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeZone": "UTC",
            },
        }
        if calendar_id:
            post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events"
        else:
            post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events"
        res = requests.post(post_url, headers=headers, json=event_payload, timeout=10)
        res.raise_for_status()
        event_data = res.json()
        event_id = event_data.get("id")
        print_info(f"M365 event injected successfully: {event_id}")

        # 3. Clean up the injected event
        print_info(f"Deleting the injected M365 event: {event_id}...")
        if calendar_id:
            delete_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events/{event_id}"
        else:
            delete_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
        res = requests.delete(delete_url, headers=headers, timeout=10)
        res.raise_for_status()
        print_info("M365 temporary event deleted successfully.")

        print_pass("M365 Token and Connection Test succeeded.")
        return True
    except SystemExit:
        raise
    except Exception as e:
        print_fail(f"M365 Token and Connection Test failed: {e}")
        return False


def get_csrf_session(url):
    """
    Performs a GET request to the handle-booking URL to retrieve a CSRF token and cookie,
    and returns a requests.Session pre-configured with X-CSRF-Token and cookies.
    """
    session = requests.Session()
    print_info(f"Fetching CSRF token from {url}...")
    res = session.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    csrf_token = data.get("csrf_token")
    if not csrf_token:
        raise Exception("Failed to retrieve CSRF token from GET response.")
    session.headers.update({
        "X-CSRF-Token": csrf_token,
        "Origin": "https://www.bodiefoundation.org",
    })
    print_info(f"Successfully obtained CSRF token and configured session.")
    return session


def test_booking_function():
    print_info("Running Live Function - Booking Test...")
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking"
    try:
        # 1. Send OPTIONS
        headers = {
            "Origin": "https://www.bodiefoundation.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-CSRF-Token",
        }
        print_info(f"Sending OPTIONS to {url}...")
        res = requests.options(url, headers=headers, timeout=10)
        if res.status_code != 204:
            raise Exception(f"OPTIONS status code is {res.status_code}, expected 204.")

        # Verify CORS headers
        allow_origin = res.headers.get("Access-Control-Allow-Origin")
        allow_methods = res.headers.get("Access-Control-Allow-Methods")

        if not allow_origin or "bodiefoundation.org" not in allow_origin:
            raise Exception(f"Unexpected Access-Control-Allow-Origin: {allow_origin}")
        if not allow_methods or "POST" not in allow_methods:
            raise Exception(f"Unexpected Access-Control-Allow-Methods: {allow_methods}")

        print_info("CORS OPTIONS headers validated.")

        # Get CSRF session
        session = get_csrf_session(url)

        # 2. Send POST with invalid placeholder data (invalid date format)
        payload = {
            "date": "invalid-date",
            "time": "10:00",
            "party_size": 4,
            "guest": {
                "name": "Integration Test Placeholder",
                "email": "test@example.com",
                "phone": "555-0100",
            },
        }
        print_info(f"Sending POST with invalid date format to {url}...")
        res = session.post(url, json=payload, timeout=10)
        if res.status_code != 409:
            raise Exception(
                f"POST invalid date format status code is {res.status_code}, expected 409."
            )

        response_json = res.json()
        if "Invalid date format" not in response_json.get("message", ""):
            raise Exception(
                f"Unexpected response payload for invalid date: {response_json}"
            )

        # Verify CORS headers for POST response
        post_origin = res.headers.get("Access-Control-Allow-Origin")
        if not post_origin:
            raise Exception(
                "Access-Control-Allow-Origin header is missing in POST response."
            )

        print_pass("Live Function - Booking Test succeeded.")
        return True
    except Exception as e:
        print_fail(f"Live Function - Booking Test failed: {e}")
        return False


def test_pruning_function():
    print_info("Running Live Function - Pruning Test...")
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/prune-unpaid-slots"
    try:
        # Generate Google OIDC token programmatically
        print_info("Generating Google OIDC token...")
        credentials, project = google.auth.default()
        req = google.auth.transport.requests.Request()
        credentials.refresh(req)

        oidc_token = getattr(credentials, "id_token", None)
        if not oidc_token:
            try:
                # Most reliable method for local developer credentials
                print_info("Generating OIDC token via gcloud CLI...")
                oidc_token = (
                    subprocess.check_output(["gcloud", "auth", "print-identity-token"])
                    .decode()
                    .strip()
                )
            except Exception:
                print_info("Generating OIDC token via fetch_id_token fallback...")
                from google.oauth2 import id_token as google_id_token

                oidc_token = google_id_token.fetch_id_token(req, url)
        if not oidc_token:
            raise Exception("Failed to generate Google OIDC ID token.")

        print_info("OIDC Token generated successfully.")

        # Send POST request
        headers = {"Authorization": f"Bearer {oidc_token}"}
        print_info(f"Sending POST to prune function {url}...")
        res = requests.post(url, headers=headers, timeout=15)

        if res.status_code == 200:
            print_pass(f"Live Function - Pruning Test succeeded (200 OK): {res.text}")
            return True
        elif res.status_code in (503, 401, 403):
            print_warn(
                f"Pruning Test: function returned status {res.status_code}. Response: {res.text}"
            )
            return False
        else:
            print_fail(
                f"Pruning Test: unexpected status code {res.status_code}. Response: {res.text}"
            )
            return False
    except Exception as e:
        print_fail(f"Live Function - Pruning Test failed: {e}")
        return False


def _get_oidc_token(url):
    """
    Generate Google OIDC token programmatically for service-to-service Cloud Function authorization.
    """
    credentials, project = google.auth.default()
    req = google.auth.transport.requests.Request()
    credentials.refresh(req)

    oidc_token = getattr(credentials, "id_token", None)
    if not oidc_token:
        try:
            # Most reliable method for local developer credentials
            oidc_token = (
                subprocess.check_output(["gcloud", "auth", "print-identity-token"])
                .decode()
                .strip()
            )
        except Exception:
            from google.oauth2 import id_token as google_id_token
            oidc_token = google_id_token.fetch_id_token(req, url)
    if not oidc_token:
        raise Exception("Failed to generate Google OIDC ID token.")
    return oidc_token


def find_live_available_slot():
    """
    Query m365-free-availability to find any live slot that is free.
    """
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-free-availability"
    try:
        print_info("Finding live available slot from m365-free-availability...")
        res = requests.get(url, timeout=15)
        if res.ok:
            data = res.json()
            dates = data.get("dates", {})
            for date_str, details in dates.items():
                slots = details.get("slots", [])
                for slot in slots:
                    dt = datetime.fromisoformat(slot)
                    time_str = dt.strftime("%H:%M")
                    print_info(f"Found live available slot: {date_str} at {time_str}")
                    return date_str, time_str
    except Exception as e:
        print_warn(f"Failed to find live available slot from m365-free-availability: {e}")
    return None, None


def test_retry_unpaid_function():
    print_info("Running Live Function - Retry Unpaid Bookings Test (Auth/CORS)...")
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/retry-unpaid-bookings"
    try:
        oidc_token = _get_oidc_token(url)
        headers = {"Authorization": f"Bearer {oidc_token}"}
        print_info(f"Sending POST to retry function {url}...")
        res = requests.post(url, headers=headers, timeout=20)

        if res.status_code == 200:
            print_pass(
                f"Live Function - Retry Unpaid Bookings Test succeeded (200 OK): {res.text}"
            )
            return True
        elif res.status_code in (503, 401, 403):
            print_warn(
                f"Retry Unpaid Bookings Test: function returned status {res.status_code}. Response: {res.text}"
            )
            return False
        else:
            print_fail(
                f"Retry Unpaid Bookings Test: unexpected status code {res.status_code}. Response: {res.text}"
            )
            return False
    except Exception as e:
        print_fail(f"Live Function - Retry Unpaid Bookings Test failed: {e}")
        return False


def test_m365_availability_and_filtering(db):
    print_info("Running M365 Availability and Filtering Integration Test...")
    event_id = None
    event_deleted = False
    date_str = "2029-12-02"
    time_str = "10:00"
    expected_slot = "2029-12-02T10:00:00-08:00"
    
    try:
        # 1. Fetch M365 auth config
        doc_ref = db.collection("config").document("m365_auth")
        doc = doc_ref.get()
        if not doc.exists:
            print_warn("M365 config/m365_auth document does not exist. Skipping availability filtering test.")
            return True

        m365_data = doc.to_dict() or {}
        access_token = m365_data.get("access_token")
        refresh_token = m365_data.get("refresh_token")
        user_id = m365_data.get("user_id")
        calendar_id = m365_data.get("calendar_id")

        if not access_token or not refresh_token or not user_id:
            print_warn("M365 tokens/user_id missing. Skipping availability filtering test.")
            return True

        # Check expiration and refresh if needed
        expires_at = m365_data.get("expires_at")
        now_utc = datetime.now(timezone.utc)
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = now_utc - timedelta(hours=1)

        if now_utc >= expires_at - timedelta(minutes=5):
            print_info("M365 token expired. Refreshing for availability filtering test...")
            client_id = m365_data.get("client_id") or os.environ.get("M365_CLIENT_ID")
            client_secret = m365_data.get("client_secret") or os.environ.get("M365_CLIENT_SECRET")
            tenant_id = m365_data.get("tenant_id") or os.environ.get("M365_TENANT_ID", "common")

            if not all([client_id, client_secret]):
                print_warn("Missing M365 credentials. Skipping availability filtering test.")
                return True

            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            res = requests.post(token_url, data=payload, timeout=10)
            res.raise_for_status()
            token_response = res.json()
            access_token = token_response.get("access_token")
            new_refresh_token = token_response.get("refresh_token")
            expires_in = token_response.get("expires_in", 3600)
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

            update_data = {
                "access_token": access_token,
                "expires_at": new_expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if new_refresh_token:
                update_data["refresh_token"] = new_refresh_token
            doc_ref.update(update_data)

        # 2. Inject temporary 'Touring Hours' event in M365 on far-future date: 2029-12-02
        event_payload = {
            "subject": "Touring Hours - Integration Test",
            "showAs": "free",
            "body": {
                "contentType": "HTML",
                "content": "Temporary Touring Hours for Integration Test",
            },
            "start": {
                "dateTime": "2029-12-02T10:00:00",
                "timeZone": "Pacific Standard Time",
            },
            "end": {
                "dateTime": "2029-12-02T11:00:00",
                "timeZone": "Pacific Standard Time",
            },
        }
        if calendar_id:
            post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events"
        else:
            post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        print_info("Injecting temporary Touring Hours event on 2029-12-02...")
        res = requests.post(post_url, headers=headers, json=event_payload, timeout=15)
        res.raise_for_status()
        event_id = res.json().get("id")
        print_pass(f"Touring Hours event injected: {event_id}")

        # 3. Query m365-free-availability and assert the slot is available
        avail_url = f"https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-free-availability?start={date_str}&end={date_str}"
        print_info(f"Querying live m365-free-availability for {date_str}...")
        res_avail = requests.get(avail_url, timeout=15)
        res_avail.raise_for_status()
        avail_data = res_avail.json()
        
        slots = avail_data.get("dates", {}).get(date_str, {}).get("slots", [])
        if expected_slot not in slots:
            raise Exception(f"Expected slot {expected_slot} not found in available slots: {slots}")
        print_pass(f"Slot {expected_slot} successfully returned as available.")

        # 4. Write fake taken slot in public/2029-12-02
        inventory_ref = db.collection("public").document(date_str)
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("America/Los_Angeles")
        dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        
        print_info(f"Setting fake taken slot in public/{date_str}...")
        inventory_ref.set({
            "taken_slots": [dt_local],
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)

        # 5. Query m365-free-availability again and assert the slot is filtered out
        print_info("Re-querying live m365-free-availability after marking slot as taken...")
        res_avail2 = requests.get(avail_url, timeout=15)
        res_avail2.raise_for_status()
        avail_data2 = res_avail2.json()
        slots2 = avail_data2.get("dates", {}).get(date_str, {}).get("slots", [])
        if expected_slot in slots2:
            raise Exception(f"Expected slot {expected_slot} to be filtered out, but it was still returned: {slots2}")
        print_pass(f"Slot {expected_slot} successfully filtered out from available slots.")
        return True

    except Exception as e:
        print_fail(f"M365 Availability and Filtering Integration Test failed: {e}")
        return False
    finally:
        # 6. Delete M365 event
        if event_id and not event_deleted:
            try:
                print_info(f"Deleting temporary Touring Hours event: {event_id}...")
                if calendar_id:
                    delete_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events/{event_id}"
                else:
                    delete_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
                del_res = requests.delete(delete_url, headers=headers, timeout=10)
                if del_res.ok:
                    print_pass("Touring Hours event deleted from calendar.")
                else:
                    print_warn(f"Failed to delete event: {del_res.text}")
            except Exception as e:
                print_warn(f"Error deleting M365 event: {e}")

        # 7. Delete Firestore public/2029-12-02 document
        try:
            print_info(f"Deleting public/{date_str} document from Firestore...")
            db.collection("public").document(date_str).delete()
            print_pass(f"public/{date_str} document deleted.")
        except Exception as e:
            print_warn(f"Error deleting Firestore public document: {e}")


def test_handle_booking_conflict(db):
    print_info("Running Live Function - Booking Conflict Test with Fake Taken Slot...")
    date_str = "2029-12-01"
    time_str = "10:00"
    try:
        inventory_ref = db.collection("public").document(date_str)
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("America/Los_Angeles")
        dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        
        # Add the slot to taken_slots
        inventory_ref.set({
            "taken_slots": [dt_local],
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)
        print_info(f"Created fake taken slot on {date_str} at {time_str}.")

        # Call handle-booking with that slot
        url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking"
        session = get_csrf_session(url)
        payload = {
            "date": date_str,
            "time": time_str,
            "party_size": 2,
            "guest": {
                "name": "Integration Test Conflict",
                "email": "test-conflict@example.com",
                "phone": "555-0100"
            }
        }
        print_info(f"Sending POST to {url} expecting conflict (409)...")
        res = session.post(url, json=payload, timeout=20)
        
        # Assert Conflict
        if res.status_code == 409:
            print_pass("Conflict (409) returned as expected for already taken slot.")
            return True
        else:
            print_fail(f"Expected status code 409, but got {res.status_code}. Response: {res.text}")
            return False
    except Exception as e:
        print_fail(f"Booking Conflict Test failed: {e}")
        return False
    finally:
        # Clean up the slot
        try:
            db.collection("public").document(date_str).delete()
            print_info(f"Cleaned up fake taken slot document public/{date_str}.")
        except Exception as e:
            print_warn(f"Failed to clean up fake taken slot document: {e}")


def test_live_pruning_workflow(db):
    print_info("Running Live Pruning End-to-End Test...")
    booking_id = "test_prune_booking"
    date_str = "2029-12-05"
    time_str = "10:00"
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/prune-unpaid-slots"
    try:
        # 1. Create fake booking document
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("America/Los_Angeles")
        
        # created_at is 50 hours ago (so booking_age > 48h TTL)
        now_utc = datetime.now(timezone.utc)
        created_at = now_utc - timedelta(hours=50)
        
        # tour_datetime is 2029-12-05 10:00 Pacific
        dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        tour_datetime = dt_local.astimezone(timezone.utc)
        
        booking_payload = {
            "payment_status": "PENDING",
            "created_at": created_at,
            "tour_datetime": tour_datetime,
            "party_size": 2,
            "guest": {
                "name": "Prune Test",
                "email": "test-prune@example.com",
                "phone": "555-1111",
            },
            "reminder_sent": 0,
        }
        
        print_info(f"Creating fake expired booking {booking_id} in Firestore...")
        db.collection("bookings").document(booking_id).set(booking_payload)
        
        # 2. Create fake taken slot in public/2029-12-05
        print_info(f"Marking slot {time_str} as taken in public/{date_str}...")
        inventory_ref = db.collection("public").document(date_str)
        inventory_ref.set({
            "taken_slots": [dt_local],
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        # 3. Request OIDC token & call prune-unpaid-slots live
        oidc_token = _get_oidc_token(url)
        headers = {"Authorization": f"Bearer {oidc_token}"}
        print_info(f"Triggering live prune function at {url}...")
        res = requests.post(url, headers=headers, timeout=20)
        res.raise_for_status()
        print_pass(f"Prune endpoint responded: {res.text}")
        
        # 4. Assert booking status updated to CANCELLED_UNPAID
        booking_doc = db.collection("bookings").document(booking_id).get()
        if not booking_doc.exists:
            raise Exception("Fake booking document disappeared!")
        status = booking_doc.to_dict().get("payment_status")
        if status != "CANCELLED_UNPAID":
            raise Exception(f"Expected booking payment_status to be 'CANCELLED_UNPAID', but got '{status}'")
        print_pass("Booking payment_status successfully updated to CANCELLED_UNPAID.")
        
        # 5. Assert slot removed from public/2029-12-05
        inventory_doc = db.collection("public").document(date_str).get()
        if inventory_doc.exists:
            taken = inventory_doc.to_dict().get("taken_slots", [])
            taken_strs = []
            for t in taken:
                if hasattr(t, "to_datetime"):
                    t_dt = t.to_datetime()
                else:
                    t_dt = t
                if isinstance(t_dt, datetime):
                    taken_strs.append(t_dt.astimezone(local_tz).strftime("%H:%M"))
            if time_str in taken_strs:
                raise Exception(f"Expected slot {time_str} to be removed from public/{date_str}, but it was still present.")
        print_pass("Slot successfully reclaimed/removed from inventory taken_slots.")
        return True
    except Exception as e:
        print_fail(f"Live Pruning Test failed: {e}")
        return False
    finally:
        # Cleanup
        print_info("Cleaning up live pruning test mock database objects...")
        try:
            db.collection("bookings").document(booking_id).delete()
        except Exception:
            pass
        try:
            db.collection("public").document(date_str).delete()
        except Exception:
            pass
        print_pass("Cleanup of live pruning test database objects complete.")


def test_live_retry_unpaid_workflow(db):
    print_info("Running Live Retry Unpaid Booking Test...")
    booking_id = "test_retry_booking"
    date_str = "2029-12-06"
    time_str = "10:00"
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/retry-unpaid-bookings"
    try:
        # 1. Create fake booking document (unpaid/pending, older than 1 hour cutoff but not expired)
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("America/Los_Angeles")
        
        now_utc = datetime.now(timezone.utc)
        created_at = now_utc - timedelta(hours=2)
        
        dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        tour_datetime = dt_local.astimezone(timezone.utc)
        
        booking_payload = {
            "payment_status": "PENDING",
            "created_at": created_at,
            "tour_datetime": tour_datetime,
            "party_size": 2,
            "guest": {
                "name": "Retry Test Customer",
                "email": "test-retry@example.com",
                "phone": "555-2222",
            },
            "retry_attempts": 1,
            "email_sent_count": 0,
        }
        
        print_info(f"Creating fake pending booking {booking_id} in Firestore...")
        db.collection("bookings").document(booking_id).set(booking_payload)
        
        # 2. Get OIDC token & post to retry endpoint
        oidc_token = _get_oidc_token(url)
        headers = {"Authorization": f"Bearer {oidc_token}"}
        print_info(f"Triggering live retry function at {url}...")
        res = requests.post(url, headers=headers, timeout=25)
        res.raise_for_status()
        print_pass(f"Retry endpoint responded: {res.text}")
        
        # 3. Assert on booking state updates
        booking_doc = db.collection("bookings").document(booking_id).get()
        if not booking_doc.exists:
            raise Exception("Fake retry booking document disappeared!")
        
        data = booking_doc.to_dict() or {}
        retry_attempts = data.get("retry_attempts", 0)
        qbo_invoice_id = data.get("integration_ids", {}).get("qbo_invoice_id")
        payment_link = data.get("payment_link")
        email_sent = data.get("email_sent")
        
        if retry_attempts != 2:
            raise Exception(f"Expected retry_attempts to be 2, but got {retry_attempts}")
        print_pass("retry_attempts successfully incremented to 2.")
        
        if not qbo_invoice_id:
            raise Exception("QBO invoice ID is missing in updated booking document!")
        print_pass(f"QBO invoice successfully generated/recreated: ID={qbo_invoice_id}")
        
        if not payment_link:
            raise Exception("payment_link is missing in updated booking document!")
        print_pass(f"payment_link successfully updated: {payment_link}")
        
        if not email_sent:
            raise Exception("email_sent flag is missing or false in updated booking document!")
        print_pass("email_sent flag successfully set to True.")
        
        return True
    except Exception as e:
        print_fail(f"Live Retry Unpaid Test failed: {e}")
        return False
    finally:
        # Cleanup
        print_info("Cleaning up live retry test booking...")
        try:
            db.collection("bookings").document(booking_id).delete()
        except Exception:
            pass
        print_pass("Cleanup of live retry test booking complete.")


def test_successful_booking(db):
    """Attempt a real booking to verify invoice and M365 event creation."""
    print_info("Running Successful Booking Test...")
    event_id = None
    m365_event_id = None
    booking_id = None
    date_str = "2029-12-07"
    time_str = "10:00"
    user_id = None
    calendar_id = None
    headers = {}
    
    try:
        # 1. Fetch M365 auth config and refresh token if needed
        doc_ref = db.collection("config").document("m365_auth")
        doc = doc_ref.get()
        if not doc.exists:
            print_warn("M365 config/m365_auth document does not exist. Skipping successful booking test.")
            return True

        m365_data = doc.to_dict() or {}
        access_token = m365_data.get("access_token")
        refresh_token = m365_data.get("refresh_token")
        user_id = m365_data.get("user_id")
        calendar_id = m365_data.get("calendar_id")

        if not access_token or not refresh_token or not user_id:
            print_warn("M365 tokens/user_id missing. Skipping successful booking test.")
            return True

        # Check expiration and refresh if needed
        expires_at = m365_data.get("expires_at")
        now_utc = datetime.now(timezone.utc)
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = now_utc - timedelta(hours=1)

        if now_utc >= expires_at - timedelta(minutes=5):
            print_info("M365 token expired. Refreshing for successful booking test...")
            client_id = m365_data.get("client_id") or os.environ.get("M365_CLIENT_ID")
            client_secret = m365_data.get("client_secret") or os.environ.get("M365_CLIENT_SECRET")
            tenant_id = m365_data.get("tenant_id") or os.environ.get("M365_TENANT_ID", "common")

            if not all([client_id, client_secret]):
                print_warn("Missing M365 credentials. Skipping successful booking test.")
                return True

            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            res = requests.post(token_url, data=payload, timeout=10)
            res.raise_for_status()
            token_response = res.json()
            access_token = token_response.get("access_token")
            new_refresh_token = token_response.get("refresh_token")
            expires_in = token_response.get("expires_in", 3600)
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

            update_data = {
                "access_token": access_token,
                "expires_at": new_expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if new_refresh_token:
                update_data["refresh_token"] = new_refresh_token
            doc_ref.update(update_data)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # 2. Inject temporary 'Touring Hours' event in M365 on far-future date: 2029-12-07
        event_payload = {
            "subject": "Touring Hours - Integration Test",
            "showAs": "free",
            "body": {
                "contentType": "HTML",
                "content": "Temporary Touring Hours for Successful Booking Test",
            },
            "start": {
                "dateTime": f"{date_str}T{time_str}:00",
                "timeZone": "Pacific Standard Time",
            },
            "end": {
                "dateTime": f"{date_str}T11:00:00",
                "timeZone": "Pacific Standard Time",
            },
        }
        
        if calendar_id:
            post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events"
        else:
            post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events"

        print_info(f"Injecting temporary Touring Hours event on {date_str}...")
        res = requests.post(post_url, headers=headers, json=event_payload, timeout=15)
        res.raise_for_status()
        event_id = res.json().get("id")
        print_pass(f"Touring Hours event injected: {event_id}")

        # Sleep briefly to allow API / propagation sync
        time.sleep(3)

        # 3. Call handle-booking with that slot
        payload = {
            "date": date_str,
            "time": time_str,
            "party_size": 1,
            "guest": {
                "name": "Integration Test Customer",
                "email": "test-integration@example.com",
                "phone": "555-0101",
            },
        }
        url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking"
        session = get_csrf_session(url)
        print_info(f"Sending booking POST for {date_str} at {time_str} to {url}...")
        res = session.post(url, json=payload, timeout=30)
        if not res.ok:
            raise Exception(
                f"Booking request failed with status {res.status_code}: {res.text}"
            )
        result = res.json()
        if result.get("status") != "success":
            raise Exception(f"Booking API returned error: {result}")
            
        payment_link = result.get("payment_link")
        if not payment_link:
            raise Exception("Payment link missing in successful booking response.")
        booking_id = result.get("booking_id")
        if not booking_id:
            raise Exception("Booking ID missing in response.")
        print_pass(f"Booking succeeded. ID={booking_id}, payment_link={payment_link}")
        
        # 4. Verify Firestore booking document contains integration IDs
        booking_doc = db.collection("bookings").document(booking_id).get()
        if not booking_doc.exists:
            raise Exception("Booking document not found in Firestore.")
        booking_data = booking_doc.to_dict() or {}
        integration = booking_data.get("integration_ids", {})
        m365_event_id = integration.get("m365_event_id")
        qbo_invoice_id = integration.get("qbo_invoice_id")
        
        if not qbo_invoice_id or not m365_event_id:
            raise Exception(
                f"Integration IDs (invoice: {qbo_invoice_id} or event: {m365_event_id}) missing in booking document."
            )
        print_pass(f"Invoice ({qbo_invoice_id}) and M365 event ID ({m365_event_id}) successfully verified.")
        return True

    except Exception as e:
        print_fail(f"Successful Booking Test failed: {e}")
        return False
    finally:
        # 5. Clean up Microsoft Calendar Events
        if m365_event_id:
            try:
                print_info(f"Deleting booked tour calendar event: {m365_event_id}...")
                if calendar_id:
                    del_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events/{m365_event_id}"
                else:
                    del_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{m365_event_id}"
                requests.delete(del_url, headers=headers, timeout=10)
                print_pass("Booked tour event deleted from calendar.")
            except Exception as e:
                print_warn(f"Failed to delete booked event from calendar: {e}")

        if event_id:
            try:
                print_info(f"Deleting temporary Touring Hours event: {event_id}...")
                if calendar_id:
                    del_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendars/{calendar_id}/events/{event_id}"
                else:
                    del_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
                requests.delete(del_url, headers=headers, timeout=10)
                print_pass("Touring Hours event deleted from calendar.")
            except Exception as e:
                print_warn(f"Failed to delete Touring Hours event: {e}")

        # 6. Delete Firestore booking document
        if booking_id:
            try:
                print_info(f"Deleting fake booking document {booking_id} from Firestore...")
                db.collection("bookings").document(booking_id).delete()
                print_pass("Booking document deleted.")
            except Exception as e:
                print_warn(f"Failed to delete booking document: {e}")

        # 7. Delete Firestore public inventory document
        try:
            print_info(f"Deleting public/{date_str} document from Firestore...")
            db.collection("public").document(date_str).delete()
            print_pass(f"public/{date_str} document deleted.")
        except Exception as e:
            print_warn(f"Error deleting Firestore public document: {e}")


def main():
    # Initialize Firestore Client
    try:
        db = firestore.Client(database="bodie-tours")
    except Exception as e:
        print_fail(f"Could not initialize Firestore Client: {e}")
        sys.exit(1)

    # Run tests sequentially
    tests = [
        ("Firestore Core Operations", lambda: test_firestore(db)),
        ("QBO Integration Connection", lambda: test_qbo(db)),
        ("M365 Integration Connection", lambda: test_m365(db)),
        ("Live Booking Endpoint CORS/Validation", test_booking_function),
        ("Live Pruning Cloud Scheduler Auth", test_pruning_function),
        ("Live Retry Cloud Scheduler Auth", test_retry_unpaid_function),
        ("M365 Availability and Filtering Flow", lambda: test_m365_availability_and_filtering(db)),
        ("Live Booking Double-Booking Conflict Detection", lambda: test_handle_booking_conflict(db)),
        ("Live Pruning Scheduled Maintenance Execution", lambda: test_live_pruning_workflow(db)),
        ("Live Retry Unpaid Scheduled Maintenance Execution", lambda: test_live_retry_unpaid_workflow(db)),
        ("Live Happy Path E2E Booking Transaction", lambda: test_successful_booking(db)),
    ]

    failed = False
    for name, test_func in tests:
        try:
            print_info(f"=== Running test: {name} ===")
            success = test_func()
            if not success:
                failed = True
        except SystemExit as se:
            raise se
        except Exception as e:
            print_fail(f"Unexpected error running {name} test: {e}")
            failed = True
        print()

    if failed:
        print_fail("Some verification checks failed.")
        sys.exit(2)

    print_pass("All verification checks passed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
