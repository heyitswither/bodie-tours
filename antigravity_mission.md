# Project Mission: Bodie State Park Serverless Booking System

[Context State]
Active Project: Custom Serverless State Park Tour Booking System
Target Stack: Squarespace Custom JS -> Google Cloud Functions (Python) -> Firestore DB
Database Architecture: Dual-Collection (Private Bookings + Public Inventory)
Project ID: bodie-tours-prod
Pending Decisions: Migration to Google Antigravity orchestration.

[Project State]
Objective: Export the current project architecture, codebase, and state for ingestion by Google Antigravity.
Current Task: Generating the Antigravity initialization artifact.
Pending Items:
  - Antigravity agent to write QBO invoice generation script.
  - Antigravity agent to write M365 Calendar sync script.

## Architecture Overview
You are tasked with completing a serverless, event-driven tour booking pipeline on Google Cloud Platform.
* **Project ID:** `bodie-tours-prod`
* **Region:** `us-west2`
* **Backend:** Google Cloud Functions (Python 3.12)
* **Database:** Firestore Native Mode
* **Frontend:** Custom JavaScript injected via Squarespace Code Block
* **Integrations:** QuickBooks Online (QBO) for invoicing, Microsoft 365 Graph API for availability and outlook calendar events, outlook integration for email confirmations.

## Current State & Completed Artifacts
1. **Infrastructure:** The GCP project is initialized. `gcloud` and Firebase CLI are configured. 
2. **Database:** Firestore is active. We are using a dual-collection NoSQL schema:
   * `/bookings/{bookingId}`: Private collection storing PII (Name, Email, Phone), `party_size`, `tour_datetime`, and `payment_status`.
   * `/public_inventory/{date}`: A public materialized view tracking aggregate taken slots.
3. **Security Rules:** `firestore.rules` are deployed. The `bookings` collection is hard-locked (`allow read, write: if false;`), relying entirely on the Cloud Function's admin service account to bypass rules and write data. `public_inventory` allows public reads but no client-side writes.
4. **Core Logic:** A Python Cloud Function (`main.py`) exists that handles incoming Squarespace POST requests and performs a strict **Firestore Transaction** to atomically update both collections, preventing race conditions and double-bookingx .

## Database schema

bookings collection:

"YAHfkswzEIBg72fZzXXJ (documentId)":
    "created\_at": June 3, 2026 at 5:55:19.550 PM UTC-7 (timestamp)
    "guest": (map)
        "email": "jane.doe@example.com" (string)
        "name": "Jane Doe" (string)
        "phone": "555-0199" (string)
    "integration\_ids": (map)
        "m365\_event\_id": "AAMkADh..." (string)
        "qbo\_invoice\_id": "INV-1042" (string)
    "party\_size": 4 (int64)
    "payment\_status": "PENDING" (string)
    "tour\_datetime": June 3, 2029 at 5:53:09.453 PM UTC-7 (timestamp)

public collection:

"2029-06-03": (documentId)
    "date": "2029-06-03" (string)
    "last\_updated": June 3, 2026 at 6:04:32.438 PM UTC-7 (timestamp)
    "taken\_slots": (array)
        0: June 3, 2029 at 6:00:00.000 PM UTC-7 (timestamp) 

## Your Pending Tasks (Agent Objectives)

**Task 1: The QuickBooks Online Integration**
* Expand the `main.py` success block. Upon a successful Firestore transaction, authenticate with the QBO API.
* Generate a `SalesReceipt` or `Invoice` for the `party_size` total.
* Return the secure QBO payment link in the JSON response back to the Squarespace frontend.
* *Constraint:* Utilize Google Cloud Secret Manager or Firestore to handle the QBO OAuth2 refresh token lifecycle. 

**Task 2: The Microsoft 365 Graph API Integration**
* Write a function to check the Park Ranger's M365 Outlook calendar for available slot event before allowing a booking transaction to proceed.
* Upon a successful booking, inject a pending calendar event into the M365 calendar.

**Task 3: The Pruning Service (Cron)**
* Write a new Cloud Function (`prune_unpaid_slots`) triggered by Google Cloud Scheduler every 15 minutes. 
* *Logic:* Query Firestore for documents where `payment_status == 'PENDING'` and cancel them if they exceed strict TTL limits (e.g., unpaid 3 hours after creation for next-day tours).

## Agent Directives
* Strictly use Python 3.12.
* Always use `functions-framework` and `google-cloud-firestore`.
* Provide code updates as unified `diff -u` patches where possible.
* Verify your infrastructure plans before executing writes.

Reference project planning conversation with Gemini
Read this conversation history to understand the architecture. Let me know when you've ingested it. (https://gemini.google.com/share/8f08a614078f) Ask me if you have an questions or need clarification on anything.
