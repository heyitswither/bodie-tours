import functions_framework
from google.cloud import firestore

# Initialize Firestore
db = firestore.Client()

# Hardcoded capacity for state park tours
MAX_CAPACITY = 20

@firestore.transactional
def process_booking_transaction(transaction, inventory_ref, date_str, time_str, party_size, customer_data):
    """
    Executes an atomic read-modify-write operation to prevent double-booking.
    """
    # 1. Read the current public inventory
    snapshot = inventory_ref.get(transaction=transaction)
    
    if not snapshot.exists:
        # Initialize daily inventory document if it's the first booking of the day
        inventory_data = {"date": date_str, "slots": {}, "last_updated": firestore.SERVER_TIMESTAMP}
        current_taken = 0
    else:
        inventory_data = snapshot.to_dict()
        # Safely extract the current taken count, defaulting to 0
        current_taken = inventory_data.get("slots", {}).get(time_str, {}).get("taken", 0)

    # 2. Hard capacity check
    if current_taken + party_size > MAX_CAPACITY:
        raise ValueError(f"Only {MAX_CAPACITY - current_taken} slots remaining.")

    # 3. Calculate new inventory values
    new_taken = current_taken + party_size
    new_status = "SOLD_OUT" if new_taken >= MAX_CAPACITY else "AVAILABLE"

    # 4. Stage Write 1: Update public inventory
    slots_data = inventory_data.get("slots", {})
    slots_data[time_str] = {"taken": new_taken, "status": new_status}
    transaction.set(inventory_ref, {"slots": slots_data, "last_updated": firestore.SERVER_TIMESTAMP}, merge=True)

    # 5. Stage Write 2: Create the private booking record
    new_booking_ref = db.collection("bookings").document() # Generates a random Auto-ID
    booking_payload = {
        # Using Pacific Time (-07:00) for the Nevada region
        "tour_datetime": f"{date_str}T{time_str}:00-07:00", 
        "party_size": party_size,
        "payment_status": "PENDING",
        "created_at": firestore.SERVER_TIMESTAMP,
        "customer": customer_data,
        "integration_ids": {
            "qbo_invoice_id": None,
            "m365_event_id": None
        }
    }
    transaction.set(new_booking_ref, booking_payload)
    
    return new_booking_ref.id

@functions_framework.http
def handle_booking(request):
    """
    HTTP entry point triggered by Squarespace JavaScript.
    """
    # --- CORS Configuration ---
    # Crucial for allowing Squarespace's domain to access your GCP endpoint securely.
    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)

    headers = {'Access-Control-Allow-Origin': '*'}
    
    try:
        # Parse JSON payload from Squarespace
        request_json = request.get_json(silent=True)
        date_str = request_json['date']           # e.g., "2026-06-15"
        time_str = request_json['time']           # e.g., "10:00"
        party_size = int(request_json['party_size'])
        customer_data = request_json['customer']

        inventory_ref = db.collection("public").document(date_str)
        
        # Initialize and execute the transaction
        transaction = db.transaction()
        booking_id = process_booking_transaction(
            transaction, inventory_ref, date_str, time_str, party_size, customer_data
        )

        return ({"status": "success", "booking_id": booking_id}, 200, headers)

    except ValueError as e:
        # User requested more tickets than available
        return ({"status": "error", "message": str(e)}, 409, headers)
    except Exception as e:
        # General payload or server error
        return ({"status": "error", "message": "Failed to process payload."}, 500, headers)
