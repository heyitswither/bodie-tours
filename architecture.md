Executive Summary

This report documents the architecture of the heyitswither/bodie-tours repository (Python-based booking backend + booking widget). The service is implemented as Google Cloud Functions (Python, functions-framework) using Firestore for persistent state. Key integrations: Microsoft 365 (calendar + mail) and QuickBooks Online (invoicing). Scheduled maintenance (pruning, retry logic) runs via Cloud Scheduler and is implemented in prune_unpaid_slots.py and retry_unpaid_bookings.py. Local development uses DummyFirestore and functions-framework dev server guidance in .github/copilot-instructions.md.

Confidence: 0.90 (evidence-backed; some inferences noted below)

Architecture Overview

- Runtime & deployment
  - Python 3.10 Cloud Functions (gcloud deploy script uses --runtime=python310)[^1]
  - Entry points: handle_booking, prune_unpaid_slots, retry_unpaid_bookings, qbo_login/callback (main.py and separate modules)[^2]
  - Deploy script: deploy_functions.sh performs gcloud deploy and sets OIDC audiences and Cloud Scheduler jobs[^3]

- Data storage
  - Firestore as primary datastore; database name 'bodie-tours' referenced in firebase.json and client initialization[^4]
  - Public inventory docs under collection 'public' (document id = date string YYYY-MM-DD) with fields: date, taken_slots (list), last_updated, and legacy 'slots' dict fallback[^5]
  - Private bookings under 'bookings' with fields: tour_datetime (tz-aware UTC), party_size, payment_status, reminder_sent, created_at (server timestamp), guest, integration_ids (qbo_invoice_id, m365_event_id), token, payment_link[^6]

- Core components
  - Booking Handler (main.handle_booking): validates input, checks M365 availability, reserves slot via process_booking_transaction (transactional), creates booking doc, then asynchronously/in-sequence creates QBO invoice and M365 calendar event, and updates booking integration_ids. Robust rollback on integration failure deletes booking and reverts inventory taken_slots[^7]
  - Inventory transaction (main.process_booking_transaction): transactional reserve using firestore.transactional; normalizes taken_slots and prevents double-booking by comparing America/Los_Angeles local keys, stores dt_local_utc into taken_slots[^8]
  - Pruning (prune_unpaid_slots.prune_unpaid_slots): scheduled HTTP endpoint; computes TTL by lead time (calculate_ttl), sends reminder emails at half-TTL via send_outlook_reminder, cancels bookings past TTL via process_cancellation_transaction which reclaims taken_slots and sets payment_status CANCELLED_UNPAID[^9]
  - Integration helper modules: get_calendars.py and verify_integrations.py contain M365/QBO helpers, token refresh functions, and small CLI/dev tools[^10]

- Integrations & external APIs
  - QuickBooks Online (QBO): OAuth flow, invoice creation, webhook handling; qbo_login/qbo_callback endpoints present in main.py[^11]
  - Microsoft 365 (M365): token refresh, sendMail, calendar event creation and deletion; prune and booking flows use Graph API with timeouts[^12]

- Observability & error handling
  - Extensive logging via logging.exception; many broad except Exception blocks at integration points and rollback paths[^13]
  - Timeouts added to external HTTP calls (requests post/get with timeout=10 in multiple places)[^14]

- Tests & CI
  - Suite of unit and e2e tests under tests/ that mock Firestore and external APIs; good coverage for booking logic and pruning flows (217 tests passing in last run)[^15]
  - No GitHub Actions workflows in .github/workflows; deployment uses deploy_functions.sh and Cloud Scheduler jobs are created in the script[^16]

Component Details & Evidence

Booking handler and transaction
- process_booking_transaction: heyitswither/bodie-tours:main.py:435-517[^8]
  - Normalizes taken_slots entries (ISO strings or datetimes) to America/Los_Angeles local strings for conflict detection; appends UTC datetime to taken_slots and writes booking doc with tour_datetime = dt_local_utc (Firestore Timestamp). See lines 445-513.

- handle_booking: heyitswither/bodie-tours:main.py:523-721[^7]
  - Validates date/time, checks M365 availability, starts transaction, calls process_booking_transaction, then creates QBO invoice and M365 event, writes integration_ids (qbo_invoice_id, m365_event_id) and payment_link. On integration failure, logs exception and attempts rollback: delete booking doc, revert inventory by removing matching taken_slots and updating/setting inventory doc.

Pruning & cancellation
- calculate_ttl: heyitswither/bodie-tours:prune_unpaid_slots.py:207-225[^9]
  - Lead-time-based TTL rules (>=7 days -> 48h; >=2 days -> 24h; >=1 day -> 3h; else 1h)

- process_cancellation_transaction: heyitswither/bodie-tours:prune_unpaid_slots.py:233-309[^9]
  - Transactionally checks booking payment_status == PENDING, reads public/{date} inventory, filters out taken_slots matching slot time (within 60s) using timezone normalization, writes back taken_slots (and deletes legacy slots if present), and updates booking.payment_status to CANCELLED_UNPAID.

- prune_unpaid_slots entrypoint: heyitswither/bodie-tours:prune_unpaid_slots.py:367-500[^9]
  - Iterates pending bookings older than 1 hour, computes TTL/age, sends reminders at half-TTL via send_outlook_reminder (prune_unpaid_slots.py:101-178)[^9], and cancels bookings past TTL via process_cancellation_transaction, also removing M365 events via remove_m365_event when available.

Security & Hardening
- DummyFirestore fallback: main.py shows FORCE_DUMMY_DB handling and DummyFirestore class; prune_unpaid_slots uses MagicMock fallback; tests rely on these behaviors[^4].
- HTTP timeouts: requests.post/get include timeout=10 in M365 and QBO helpers (prune_unpaid_slots._get_m365_token_for_prune uses timeout=10)[^14].
- OAuth redirect validation: main.py qbo_login/qbo_callback include state token and redirect_uri resolved via _resolve_qbo_credentials; verify_integrations.py guarded auto-open browser behavior (requires BROWSER env var)[^11].

Tests & CI Evidence
- Tests exercise nearly all primary functions, with focused files: tests/test_main_coverage_additions.py and tests/test_prune_coverage_additions.py have many cases for booking, rollback, TTL and prune behaviors[^15].
- No GitHub Actions workflows found; deployment performed by deploy_functions.sh which sets up Cloud Scheduler jobs for retry/prune every 15 minutes and configures OIDC audiences for secured functions[^16].

Confidence Assessment
- Overall confidence: 0.90. Majority of claims are directly cited from source files in the repo. Inferences:
  - Exact production deployment environment variables and IAM/service-account binding inferred from deploy script but may vary in actual infra.
  - Some runtime behaviors (Firestore timestamps vs string serialization) are inferred from code and tests — Firestore client serializes tz-aware datetimes to Timestamp but DB examples in tests sometimes use strings.

Missing / Ambiguities
- No README.md or LICENSE at repo root — some project documentation files exist (PROJECT.md, BRIEFING.md) but canonical README absent[^17].
- Not all edge-case behaviors around DST transitions and exact serialization of taken_slots (string vs datetime) are fully proven; recommend reviewing tests and live DB records[^18].
- Security: Firestore rules file exists but should be audited in place (firestore.rules present) to confirm server-only writes for sensitive fields[^5].

Recommendations / Next Steps
1. Deep review of retry_unpaid_bookings and webhook flows to ensure payment_status transitions are atomic and idempotent (prune relies on booking.payment_status values). Target: retry_unpaid_bookings.py and qbo_webhook in main.py.
2. Add unit tests around DST transition dates for slot normalization.
3. Consider strengthening rollback to handle legacy 'slots' dict updates explicitly if tests expect that schema.
4. Add README.md summarizing deployment, local dev, and test commands, and add GitHub Actions for CI to run pytest and lint on PRs.

Footnotes

[^1]: deploy_functions.sh:15-25
[^2]: main.py:523-525
[^3]: deploy_functions.sh:89-99,135-144
[^4]: main.py:17-23; firebase.json:1-6
[^5]: main.py:450-496; firestore.rules:5-15
[^6]: main.py:498-515
[^7]: main.py:523-721
[^8]: main.py:435-517
[^9]: prune_unpaid_slots.py:207-225,233-309,367-500
[^10]: get_calendars.py:1-41; verify_integrations.py:1-562
[^11]: main.py:321-379; main.py:766-796
[^12]: prune_unpaid_slots.py:52-95; prune_unpaid_slots.py:101-178
[^13]: main.py:614-643; main.py:662-709
[^14]: prune_unpaid_slots.py:79-81; prune_unpaid_slots.py:177-178; get_calendars.py:1-41
[^15]: tests/ (multiple files: tests/test_main_coverage_additions.py, tests/test_prune_coverage_additions.py, tests/e2e/...)
[^16]: deploy_functions.sh:15-25,135-144
[^17]: repo root listing: top-level directory files
[^18]: main.py:445-448,prune_unpaid_slots.py:266-276

Saved file: /home/freya/.copilot/session-state/a760d750-3153-4fd3-ad90-148f6ce36e45/research/what-is-the-architecture-of-this-codebase.md
