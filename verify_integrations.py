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
        verifier_token = auth_data.get("dev-verifier_token") or auth_data.get("dev-verify")
    else:
        client_id = auth_data.get("prod-id")
        client_secret = auth_data.get("prod-secret")
        verifier_token = auth_data.get("prod-verifier_token") or auth_data.get("prod-verify")

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
        subprocess.run(["xdg-open", login_url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            "timestamp": firestore.SERVER_TIMESTAMP
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
            auth_data = open_browser_and_poll(db, "qbo_auth", "https://us-west2-bodie-tours-prod.cloudfunctions.net/qbo-login")
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
                raise Exception("Missing QBO client credentials (client_id/client_secret) to refresh token.")
                
            token_endpoint = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
            import base64
            auth_str = f"{client_id}:{client_secret}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            
            headers = {
                "Authorization": f"Basic {b64_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"
            }
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token
            }
            
            res = requests.post(token_endpoint, headers=headers, data=data, timeout=10)
            res.raise_for_status()
            token_data = res.json()
            
            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 3600)
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            
            update_payload = {
                "access_token": new_access_token,
                "expires_at": new_expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP
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
            "Accept": "application/json"
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
            m365_data = open_browser_and_poll(db, "m365_auth", "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-login")
            access_token = m365_data.get("access_token")
            refresh_token = m365_data.get("refresh_token")
            
        user_id = m365_data.get("user_id")
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
            client_secret = m365_data.get("client_secret") or os.environ.get("M365_CLIENT_SECRET")
            tenant_id = m365_data.get("tenant_id") or os.environ.get("M365_TENANT_ID", "common")
            
            if not all([client_id, client_secret]):
                raise Exception("Missing M365 client credentials (client_id/client_secret) to refresh token.")
                
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
            
            res = requests.post(token_url, data=payload, timeout=10)
            res.raise_for_status()
            token_response = res.json()
            
            new_access_token = token_response.get("access_token")
            new_refresh_token = token_response.get("refresh_token")
            expires_in = token_response.get("expires_in", 3600)
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            
            update_data = {
                "access_token": new_access_token,
                "expires_at": new_expires_at,
                "updated_at": firestore.SERVER_TIMESTAMP
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
            "Accept": "application/json"
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
                "content": "Temporary test event for integration verification."
            },
            "start": {
                "dateTime": event_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeZone": "UTC"
            },
            "end": {
                "dateTime": event_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeZone": "UTC"
            }
        }
        post_url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendar/events"
        res = requests.post(post_url, headers=headers, json=event_payload, timeout=10)
        res.raise_for_status()
        event_data = res.json()
        event_id = event_data.get("id")
        print_info(f"M365 event injected successfully: {event_id}")
        
        # 3. Clean up the injected event
        print_info(f"Deleting the injected M365 event: {event_id}...")
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

def test_booking_function():
    print_info("Running Live Function - Booking Test...")
    url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking"
    try:
        # 1. Send OPTIONS
        headers = {
            "Origin": "https://www.bodiefoundation.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type"
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
        
        # 2. Send POST with invalid placeholder data (invalid date format)
        payload = {
            "date": "invalid-date",
            "time": "10:00",
            "party_size": 4,
            "guest": {
                "name": "Integration Test Placeholder",
                "email": "test@example.com",
                "phone": "555-0100"
            }
        }
        print_info(f"Sending POST with invalid date format to {url}...")
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 409:
            raise Exception(f"POST invalid date format status code is {res.status_code}, expected 409.")
            
        response_json = res.json()
        if "Invalid date format" not in response_json.get("message", ""):
            raise Exception(f"Unexpected response payload for invalid date: {response_json}")
            
        # Verify CORS headers for POST response
        post_origin = res.headers.get("Access-Control-Allow-Origin")
        if not post_origin:
            raise Exception("Access-Control-Allow-Origin header is missing in POST response.")
            
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
                oidc_token = subprocess.check_output(["gcloud", "auth", "print-identity-token"]).decode().strip()
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
            print_warn(f"Pruning Test: function returned status {res.status_code}. Response: {res.text}")
            return False
        else:
            print_fail(f"Pruning Test: unexpected status code {res.status_code}. Response: {res.text}")
            return False
    except Exception as e:
        print_fail(f"Live Function - Pruning Test failed: {e}")
        return False

def test_successful_booking(db):
    """Attempt a real booking to verify invoice and M365 event creation."""
    print_info("Running Successful Booking Test...")
    try:
        # Find a public date with no taken slots
        docs = db.collection("public").limit(20).stream()
        date_str = None
        for doc in docs:
            data = doc.to_dict() or {}
            taken = data.get("taken_slots", [])
            if not taken:
                date_str = doc.id
                break
        if not date_str:
            raise Exception("No available public date found for test booking.")
        # Choose a time that is likely free
        time_str = "10:00"
        # Prepare payload
        payload = {
            "date": date_str,
            "time": time_str,
            "party_size": 1,
            "guest": {
                "name": "Integration Test",
                "email": "test-integration@example.com",
                "phone": "555-0101"
            }
        }
        url = "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking"
        res = requests.post(url, json=payload, timeout=20)
        if not res.ok:
            raise Exception(f"Booking request failed with status {res.status_code}: {res.text}")
        result = res.json()
        if result.get("status") != "success":
            raise Exception(f"Booking API returned error: {result}")
        # Verify payment link present
        payment_link = result.get("payment_link")
        if not payment_link:
            raise Exception("Payment link missing in successful booking response.")
        booking_id = result.get("booking_id")
        if not booking_id:
            raise Exception("Booking ID missing in response.")
        print_pass(f"Booking succeeded. ID={booking_id}, payment_link={payment_link}")
        # Verify Firestore booking document contains integration IDs
        booking_doc = db.collection("bookings").document(booking_id).get()
        if not booking_doc.exists:
            raise Exception("Booking document not found in Firestore.")
        booking_data = booking_doc.to_dict() or {}
        integration = booking_data.get("integration_ids", {})
        if not integration.get("qbo_invoice_id") or not integration.get("m365_event_id"):
            raise Exception("Integration IDs (invoice or event) missing in booking document.")
        print_pass("Invoice and M365 event IDs present in booking record.")
        # Cleanup: delete the booking and remove the taken slot
        db.collection("bookings").document(booking_id).delete()
        # Remove taken slot from inventory
        inventory_ref = db.collection("public").document(date_str)
        # Compute the datetime that was booked (local timezone conversion mirrors main.py)
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("America/Los_Angeles")
        dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
        inventory_ref.update({"taken_slots": firestore.ArrayRemove([dt_local])})
        print_pass("Cleanup of test booking completed.")
        return True
    except Exception as e:
        print_fail(f"Successful Booking Test failed: {e}")
        return False
    
def main():
    # Initialize Firestore Client
    try:
        db = firestore.Client(database="bodie-tours")
    except Exception as e:
        print_fail(f"Could not initialize Firestore Client: {e}")
        sys.exit(1)
    
    # Run tests sequentially
    tests = [
        ("Firestore", lambda: test_firestore(db)),
        ("QBO Connection", lambda: test_qbo(db)),
        ("M365 Connection", lambda: test_m365(db)),
        ("Live Booking Endpoint", test_booking_function),
        ("Live Pruning Endpoint", test_pruning_function)
    ]
    
    failed = False
    for name, test_func in tests:
        try:
            success = test_func()
            if not success:
                failed = True
        except SystemExit as se:
            # Re-raise SystemExit to preserve warning exit codes
            raise se
        except Exception as e:
            print_fail(f"Unexpected error running {name} test: {e}")
            failed = True
            
    if failed:
        print_fail("Some verification checks failed.")
        sys.exit(2)
        
    print_pass("All verification checks passed.")
    sys.exit(0)

if __name__ == "__main__":
    main()
