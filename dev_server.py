import os
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
from google.cloud import firestore
from main import get_m365_access_token, check_m365_availability
from flask import Flask, request, jsonify

app = Flask(__name__)

db = firestore.Client(database="bodie-tours")

# Configure logging
logging.basicConfig(level=logging.INFO)

@app.route('/', methods=['GET'])
def index():
    try:
        html_path = '/home/freya/bodie-tours/booking_widget.html'
        if not os.path.exists(html_path):
            return f"Error: {html_path} does not exist", 404
            
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Replace the production Cloud Function URL with our local endpoint
        content = content.replace(
            'https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking',
            '/handle-booking'
        )
        # Replace the production Firestore URL with our local endpoint
        content = content.replace(
            'https://firestore.googleapis.com/v1/projects/bodie-tours-prod/databases/bodie-tours/documents',
            '/firestore'
        )
        # Replace template variations as well
        content = content.replace(
            'https://firestore.googleapis.com/v1/projects/${FIREBASE_PROJECT}/databases/bodie-tours/documents',
            '/firestore'
        )
        # Replace any local dev port 8081 from previous agent modifications
        content = content.replace(
            'http://localhost:8081',
            '/handle-booking'
        )
        # Direct the widget code to use the actual Firestore endpoint instead of mock_availability.json
        content = content.replace(
            "window.location.hostname === 'localhost' || window.location.port === '9010'",
            "false"
        )
        
        return content, 200, {'Content-Type': 'text/html; charset=utf-8'}
    except Exception as e:
        app.logger.error(f"Error loading booking_widget.html: {str(e)}")
        return f"Error loading booking_widget.html: {str(e)}", 500

@app.route('/firestore/public', methods=['GET', 'OPTIONS'])
def firestore_public():
    if request.method == 'OPTIONS':
        return '', 200
        
    page_size = request.args.get('pageSize')
    app.logger.info(f"Received Firestore public availability request. pageSize={page_size}")
    
    # Return mock availability for June 15, 2026
    mock_data = {
        "documents": [
            {
                "name": "projects/bodie-tours-prod/databases/bodie-tours/documents/public/2026-06-15",
                "fields": {
                    "slots": {
                        "mapValue": {
                            "fields": {
                                "10:00": {
                                    "mapValue": {
                                        "fields": {
                                            "status": {"stringValue": "AVAILABLE"},
                                            "taken": {"integerValue": "0"}
                                        }
                                    }
                                },
                                "13:00": {
                                    "mapValue": {
                                        "fields": {
                                            "status": {"stringValue": "SOLD_OUT"},
                                            "taken": {"integerValue": "0"}
                                        }
                                    }
                                },
                                "16:00": {
                                    "mapValue": {
                                        "fields": {
                                            "status": {"stringValue": "AVAILABLE"},
                                            "taken": {"integerValue": "1"}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        ]
    }
    return jsonify(mock_data)

@app.route('/mock_availability.json', methods=['GET', 'OPTIONS'])
def mock_availability():
    if request.method == 'OPTIONS':
        return '', 200
    # Delegate to firestore_public to be fully compatible
    return firestore_public()

@app.route('/handle-booking', methods=['POST', 'OPTIONS'])
@app.route('/', methods=['POST', 'OPTIONS'])
def handle_booking():
    if request.method == 'OPTIONS':
        return '', 200
        
    data = request.get_json(silent=True) or {}
    guest = data.get('guest', {})
    name = guest.get('name')
    
    app.logger.info(f"Received handle-booking request for guest: {name}")
    
    if name == "Conflict Error":
        return jsonify({ "message": "Booking conflict: The requested time slot is already taken." }), 409
    elif name == "Server Error":
        return jsonify({ "message": "Internal server error occurred while processing transaction." }), 500
    else:
        return jsonify({
            "status": "success",
            "booking_id": "BK-20260615-1000",
            "payment_link": "https://connect.intuit.com/pay/invoice/mock-link-123"
        }), 200

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response
@app.route('/m365/free-availability', methods=['GET'])
def m365_free_availability():
    """Return available tour slots based on M365 calendar 'Bodie Tours' with free status."""
    token, user_id = get_m365_access_token()
    # Expect start and end date as YYYY-MM-DD, default to today and +30 days
    from datetime import datetime, timedelta
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    today = datetime.now().date()
    start_date = datetime.strptime(start_str, '%Y-%m-%d').date() if start_str else today
    end_date = datetime.strptime(end_str, '%Y-%m-%d').date() if end_str else today + timedelta(days=30)
    # Define typical tour hours (9am‑4pm)
    hours = [f"{h:02d}:00" for h in range(9, 17)]
    result = {"dates": {}}
    current = start_date
    while current <= end_date:
        date_iso = current.isoformat()
        slots = {}
        for hour in hours:
            if check_m365_availability(token, user_id, date_iso, hour):
                slots[hour] = {"status": "AVAILABLE", "taken": 0}
        if slots:
            result["dates"][date_iso] = {"slots": slots}
        current += timedelta(days=1)
    return jsonify(result)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8082)
