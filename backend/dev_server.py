import os
import logging
from datetime import datetime, timedelta
from google.cloud import firestore
from main import get_m365_access_token, check_m365_availability
from flask import Flask, request, jsonify

app = Flask(__name__)

db = firestore.Client(database="bodie-tours")

# Configure logging
logging.basicConfig(level=logging.INFO)


@app.route("/", methods=["GET"])
def index():
    try:
        html_path = "/home/freya/bodie-tours/booking_widget.html"
        if not os.path.exists(html_path):
            return f"Error: {html_path} does not exist", 404

        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Replace the production Cloud Function URL with our local endpoint
        content = content.replace(
            "https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking",
            "/handle-booking",
        )
        # Replace the production M365 Availability URL with our local endpoint
        content = content.replace(
            "https://us-west2-bodie-tours-prod.cloudfunctions.net/m365-free-availability",
            "/m365-free-availability",
        )
        # Replace the production cancel-tour URL with our local endpoint
        content = content.replace(
            "https://us-west2-bodie-tours-prod.cloudfunctions.net/cancel-tour",
            "/cancel-tour",
        )

        # Replace any local dev port 8081 from previous agent modifications
        content = content.replace("http://localhost:8081", "/handle-booking")
        # Direct the widget code to use the actual Firestore endpoint instead of mock_availability.json
        content = content.replace(
            "window.location.hostname === 'localhost' || window.location.port === '9010'",
            "false",
        )

        return content, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        app.logger.error(f"Error loading booking_widget.html: {str(e)}")
        return f"Error loading booking_widget.html: {str(e)}", 500


@app.route("/firestore/public", methods=["GET", "OPTIONS"])
def firestore_public():
    if request.method == "OPTIONS":
        return "", 200

    page_size = request.args.get("pageSize")
    app.logger.info(
        f"Received Firestore public availability request. pageSize={page_size}"
    )

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
                                            "taken": {"integerValue": "0"},
                                        }
                                    }
                                },
                                "13:00": {
                                    "mapValue": {
                                        "fields": {
                                            "status": {"stringValue": "SOLD_OUT"},
                                            "taken": {"integerValue": "0"},
                                        }
                                    }
                                },
                                "16:00": {
                                    "mapValue": {
                                        "fields": {
                                            "status": {"stringValue": "AVAILABLE"},
                                            "taken": {"integerValue": "1"},
                                        }
                                    }
                                },
                            }
                        }
                    }
                },
            }
        ]
    }
    return jsonify(mock_data)


@app.route("/mock_availability.json", methods=["GET", "OPTIONS"])
def mock_availability():
    if request.method == "OPTIONS":
        return "", 200
    # Delegate to firestore_public to be fully compatible
    return firestore_public()


@app.route("/handle-booking", methods=["POST", "OPTIONS"])
@app.route("/", methods=["POST", "OPTIONS"])
def handle_booking():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json(silent=True) or {}
    guest = data.get("guest", {})
    name = guest.get("name")

    app.logger.info(f"Received handle-booking request for guest: {name}")

    if name == "Conflict Error":
        return (
            jsonify(
                {
                    "message": "Booking conflict: The requested time slot is already taken."
                }
            ),
            409,
        )
    elif name == "Server Error":
        return (
            jsonify(
                {
                    "message": "Internal server error occurred while processing transaction."
                }
            ),
            500,
        )
    else:
        return (
            jsonify(
                {
                    "status": "success",
                    "booking_id": "BK-20260615-1000",
                    "payment_link": "https://connect.intuit.com/pay/invoice/mock-link-123",
                }
            ),
            200,
        )


@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response


@app.route("/m365-free-availability", methods=["GET"])
def m365_free_availability():
    """Return available tour slots based on M365 calendar 'Bodie Tours' with free status.
    To ensure 100% stable, offline-capable local testing and Puppeteer simulations,
    this endpoint returns a robust mock date mapping for the June 2026 target test window.
    June 15 contains an available morning slot, a sold out afternoon slot, and an available late afternoon slot.
    July 2026 remains completely empty to verify graceful degradation.
    """
    mock_response = {
        "dates": {
            "2026-06-15": {
                "slots": {
                    "10:00": {"status": "AVAILABLE", "taken": 0},
                    "13:00": {"status": "SOLD_OUT", "taken": 0},
                    "16:00": {"status": "AVAILABLE", "taken": 1}
                }
            }
        }
    }
    return jsonify(mock_response)


@app.route("/cancel-tour", methods=["GET", "OPTIONS"])
def cancel_tour():
    if request.method == "OPTIONS":
        return "", 200
    booking_id = request.args.get("booking_id")
    token = request.args.get("token")
    if not booking_id or not token:
        return jsonify({"status": "error", "message": "Missing booking_id or token"}), 400
    return jsonify({"status": "success", "message": f"Booking {booking_id} cancelled successfully!"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)
