# Bodie State Park Serverless Booking System

## Tech Stack & Environment
- **Backend:** Python 3.12 executing on Google Cloud Functions (Gen 2).
- **Database:** Google Cloud Firestore (Native Mode).
- **Frontend:** Squarespace Custom JavaScript (`fetch` API).
- **Integrations:** QuickBooks Online (OAuth 2.0), Microsoft 365 Graph API (App-Only Auth).
- **Cloud Region:** `us-west2`
- **Project ID:** `bodie-tours-prod`

## Architecture & Data Rules
- **Stateless Execution:** Serverless functions are ephemeral. Store all operational state and transaction IDs exclusively in Firestore.
- **Dual-Collection Schema:** Maintain a strict separation between `/bookings/{id}` (private customer PII) and `/public/{date}` (publicly readable aggregated capacity).
- **Atomic Concurrency:** All modifications to bookings and public must use `@firestore.transactional` to prevent race conditions. There is always no more than one group per tour.
- **Data Air-Gap:** Never mix PII into the public inventory collection.

## Coding Conventions
- **Language:** Strictly follow PEP8 standards for Python. Use standard `google-cloud-firestore` and `functions-framework` libraries.
- **Security:** Never hardcode API credentials. Retrieve `m365-client-secret` and `qbo-client-secret` via firestore db config collection `m365_auth` and `qbo_auth`.
- **Error Handling:** External APIs (Intuit, Microsoft) will fail. Handle network timeouts gracefully and ensure Firestore transactions rollback safely if a downstream API call drops.
- **CORS:** Ensure the Cloud Function HTTP triggers explicitly handle preflight OPTIONS requests to allow secure cross-origin communication from the Squarespace domain.
