#!/usr/bin/env bash
set -euo pipefail

# Ensure screenshot directory exists
SCREENSHOT_DIR="/home/freya/bodie-tours/screenshots"
mkdir -p "$SCREENSHOT_DIR"

# Kill any existing backend or static server processes
pkill -f "functions-framework --target=handle_booking" || true
pkill -f "python -m http.server 8000" || true
pkill -f "python -m http.server 8001" || true
pkill -f "gunicorn" || true

# Start backend server on port 8081
functions-framework --target=handle_booking --port=8081 &
BACKEND_PID=$!

# Start static server on port 8000
python -m http.server 8000 --directory . &
STATIC_PID=$!

# Wait for servers to start
sleep 5
set +e

# Helper for Chrome DevTools MCP commands
cdp() {
  # Use npx with -y to auto-approve package installation and avoid global permission issues
  npx -y chrome-devtools-mcp@latest --auto-connect --chrome-arg="--no-sandbox" "$@"
}

BASE_URL="http://localhost:8000/booking_widget.html"
# Launch Chrome for MCP automation
cdp launch
# brief pause to ensure servers are ready
sleep 1
# Open a new page for automation
# cdp new_page  # Removed; not needed

# Scenario 1: Happy Path
cdp navigate_page --url "$BASE_URL"
cdp wait_for --selector "#bodie-booking-widget" --timeout 5000
# ensure page fully loaded
sleep 2
cdp click --selector ".bb-day.available" || true
cdp click --selector "#bb-to-step-2"
cdp wait_for --selector ".bb-slot:not(.full)" --timeout 5000
cdp click --selector ".bb-slot:not(.full)" || true
cdp click --selector "#bb-to-step-3"
cdp fill --selector "#guest-name" --value "Test User"
cdp fill --selector "#guest-email" --value "test@example.com"
cdp fill --selector "#guest-phone" --value "555-1234"
cdp fill --selector "#guest-party" --value "2"
cdp click --selector "#bb-to-step-4"
cdp take_screenshot --filePath "$SCREENSHOT_DIR/happy_path.png"
cdp list_console_messages > "$SCREENSHOT_DIR/happy_path_console.log"
cdp list_network_requests > "$SCREENSHOT_DIR/happy_path_network.json"

# Scenario 2: Sold Out Handling
cdp navigate_page --url "$BASE_URL"
cdp wait_for --selector "#bodie-booking-widget" --timeout 5000
cdp click --selector ".bb-day.unavailable" || true
cdp take_screenshot --filePath "$SCREENSHOT_DIR/sold_out.png"
cdp list_console_messages > "$SCREENSHOT_DIR/sold_out_console.log"
cdp list_network_requests > "$SCREENSHOT_DIR/sold_out_network.json"

# Scenario 3: Empty Month
cdp navigate_page --url "$BASE_URL"
cdp wait_for --selector "#bodie-booking-widget" --timeout 5000
for i in {1..12}; do
  cdp click --selector "button.bb-month-btn" || true
  sleep 0.2
done
cdp take_screenshot --filePath "$SCREENSHOT_DIR/empty_month.png"
cdp list_console_messages > "$SCREENSHOT_DIR/empty_month_console.log"
cdp list_network_requests > "$SCREENSHOT_DIR/empty_month_network.json"

# Scenario 4: Validation Failures
cdp navigate_page --url "$BASE_URL"
cdp wait_for --selector "#bodie-booking-widget" --timeout 5000
cdp click --selector ".bb-day.available" || true
cdp click --selector "#bb-to-step-2"
cdp click --selector ".bb-slot:not(.full)" || true
cdp click --selector "#bb-to-step-3"
cdp fill --selector "#guest-name" --value ""
cdp fill --selector "#guest-email" --value "invalid-email"
cdp fill --selector "#guest-phone" --value "555-1234"
cdp fill --selector "#guest-party" --value "2"
cdp click --selector "#bb-to-step-4" || true
cdp take_screenshot --filePath "$SCREENSHOT_DIR/validation_failure.png"
cdp list_console_messages > "$SCREENSHOT_DIR/validation_failure_console.log"
cdp list_network_requests > "$SCREENSHOT_DIR/validation_failure_network.json"

# Scenario 5: Backend Error (force error)
export FORCE_ERROR=1
cdp navigate_page --url "$BASE_URL"
cdp wait_for --selector "#bodie-booking-widget" --timeout 5000
cdp click --selector ".bb-day.available" || true
cdp click --selector "#bb-to-step-2"
cdp click --selector ".bb-slot:not(.full)" || true
cdp click --selector "#bb-to-step-3"
cdp fill --selector "#guest-name" --value "Test User"
cdp fill --selector "#guest-email" --value "test@example.com"
cdp fill --selector "#guest-phone" --value "555-1234"
cdp fill --selector "#guest-party" --value "2"
cdp click --selector "#bb-to-step-4"
cdp take_screenshot --filePath "$SCREENSHOT_DIR/backend_error.png"
cdp list_console_messages > "$SCREENSHOT_DIR/backend_error_console.log"
cdp list_network_requests > "$SCREENSHOT_DIR/backend_error_network.json"

# Cleanup
kill $BACKEND_PID || true
kill $STATIC_PID || true
exit 0
