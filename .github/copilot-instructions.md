Build / Test / Lint

- Setup virtualenv and deps:
  - python -m venv .venv && source .venv/bin/activate
  - pip install -r requirements-dev.txt

- Run unit tests:
  - pytest -q
  - Run a single test: pytest tests/<path_to_test>.py::test_name  (or -k "substring")

- Run E2E tests:
  - pytest tests/e2e/ -v
  - Run a single tier: pytest tests/e2e/tier1_feature/ -v

- Run local dev server (serves booking_widget.html + mock endpoints):
  - python dev_server.py
  - Default: http://localhost:8082/

- Run a Cloud Function locally (functions-framework):
  - functions-framework --target=handle_booking --port=8081 --debug

High-level architecture

- Runtime: Python 3.12 Cloud Functions (HTTP triggers) using functions-framework for local runs.
- Primary entrypoints (main.py): handle_booking, qbo_login, qbo_callback, m365_login, m365_callback.
- Data store: Firestore with these logical collections:
  - config: persistent integration configs (qbo_auth, m365_auth)
  - public: per-date public inventory documents used to show availability
  - bookings: private booking documents (PII)
- Booking flow overview:
  1. Frontend (booking_widget.html) POSTs to /handle-booking.
  2. Service checks M365 availability (calendar "Touring Hours").
  3. Firestore transaction reserves the slot (public collection) and creates bookings/* (private).
  4. QBO invoice is created and M365 calendar event injected; booking doc updated with integration IDs.

Key conventions and repo-specific patterns

- Dual-collection schema: public vs bookings — public must never contain PII (see CONVENTIONS.md).
- Atomic Firestore transactions: use @firestore.transactional for modifications that must be atomic (one-group-per-slot rule).
- Timezone: America/Los_Angeles is used (ZoneInfo) for slot calculations and stored timestamps.
- Local testing fallbacks:
  - If Firestore credentials are not available, main.py falls back to an in-memory DummyFirestore.
  - FORCE_DUMMY_DB=1 forces dummy DB behavior for local tests.
- Config storage: QBO and M365 client tokens/refresh tokens are persisted in config/qbo_auth and config/m365_auth. Env vars can override values (e.g., M365_CLIENT_ID, QBO_CLIENT_ID, QBO_REDIRECT_URI).
- Limits and rules embedded in code:
  - MAX_GROUP_SIZE = 20
  - One-group-per-slot: slots are reserved as datetime objects in public/{date} documents.
- CORS: handle_booking enforces a small allowlist and supports OPTIONS preflight.

Useful files

- booking_widget.html — frontend widget (root)
- main.py — function implementations and integration logic
- dev_server.py — local static server + mock endpoints
- tests/ and tests/e2e/ — unit and E2E test suites
- CONVENTIONS.md, TEST_INFRA.md, PROJECT.md — project-specific rules and test strategy

Notes for Copilot sessions

- Prefer editing main.py only when requested; many functions interact with external APIs and Firestore.
- For local runs/tests, prefer using FORCE_DUMMY_DB=1 or dev_server.py to avoid requiring cloud credentials.
- When adding integration tests, persist and load sample config docs under config/ so tests remain deterministic.

---
Generated from repository files: CONVENTIONS.md, TEST_INFRA.md, PROJECT.md, main.py, dev_server.py, booking_widget.html
