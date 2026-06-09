import requests
from google.cloud import firestore

def fetch_m365_calendars():
    print("Connecting to Firestore...")
    db = firestore.Client(database="bodie-tours")
    
    auth_doc = db.collection("config").document("m365_auth").get()
    if not auth_doc.exists:
        print("Error: m365_auth document not found in config collection.")
        return

    access_token = auth_doc.to_dict().get("access_token")
    if not access_token:
        print("Error: No access_token found in m365_auth. Please run the OAuth flow.")
        return

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    print("Querying Microsoft Graph API...\n")
    res = requests.get("https://graph.microsoft.com/v1.0/me/calendars", headers=headers, timeout=10)
    
    if res.status_code != 200:
        print(f"API Error {res.status_code}: {res.text}")
        return
        
    calendars = res.json().get("value", [])
    
    print(f"{'Calendar Name':<35} | {'Calendar ID'}")
    print("-" * 120)
    for cal in calendars:
        name = cal.get("name", "Unknown")
        cal_id = cal.get("id", "No ID")
        print(f"{name:<35} | {cal_id}")

if __name__ == "__main__":
    fetch_m365_calendars()
