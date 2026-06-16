# Project Scope for Bodie State Park Booking System

## Overview
The Bodie State Park Booking System is a serverless backend integration that manages visitor tours, reserves inventory in Firestore, creates invoices via QuickBooks Online (QBO), manages scheduling/events in Microsoft 365 (M365), and automatically prunes unpaid or pending slots. This project delivers a fully hardened, production-ready backend alongside the static frontend booking widget.

## Milestones

| # | Milestone Name | Scope | Status |
|---|----------------|-------|--------|
| 1 | Database & Codebase Standardization | Transition from legacy `slots` dict to strict unified `taken_slots` array schema across all reservation and pruning transactions. Standardize `tour_datetime` to Firestore Timestamp objects (tz-aware UTC) and use Pacific timezone America/Los_Angeles globally. | **🟢 COMPLETED** |
| 2 | QBO & M365 Email/Notification Hardening | Enforce caps of at most 2 reminders, with the second reminder sent at quarter TTL. Integrate dynamic cancellation links in receipt and reminder emails, and attach `.ics` calendar invites on successful booking receipts. | **🟢 COMPLETED** |
| 3 | Security Review Remediation | Remediate all 7 security findings from `SECURITY-REVIEW.md` (HTML injection in calendar events and customer/reminder emails, exact-match CORS origins, sensitive response body leakage, strict redirect_uri validation, and CSRF protection). | **🟢 COMPLETED** |
| 4 | Token Expiration Robustness | Prevent offset-naive comparison failures by converting naive Firestore timestamps to tz-aware UTC datetimes prior to validation checks, ensuring zero-exception automatic token refreshes. | **🟢 COMPLETED** |
| 5 | Test Coverage & Visual Verification | Secure 100% statement coverage for main modules (`main.py` and `prune_unpaid_slots.py`), expand the test suite to 250 passing tests, and visually verify frontend widget behaviors (Happy Path, Sold Out, Validation Errors, and Conflicts) using Chrome DevTools. | **🟢 COMPLETED** |

## Core Contracts & Interfaces
- **Front-end URL:** `http://localhost:8000/booking_widget.html`
- **Backend API Base:** `http://localhost:8081/` (REST endpoints: `handle-booking`, `cancel-tour`, `qbo-login`, `qbo-callback`, `m365-login`, `m365-callback`, `retry-unpaid-bookings`, etc.)
- **Named Database:** Dedicated Firestore instance named `bodie-tours`

## Repository Structure
- `booking_widget.html` – Front-end widget entry point
- `main.py` – Primary Google Cloud Function endpoint for reservations, CORS, webhooks, and QBO/M365 OAuth
- `prune_unpaid_slots.py` – Scheduled Google Cloud Function endpoint for calculating TTLs, sending reminders, and transactionally releasing abandoned reservation slots
- `retry_unpaid_bookings.py` – Background job to retry and rebuild failed invoicing links
- `verify_integrations.py` – Operations script to test, authorize, and verify third-party API configurations
- `tests/` – Python unit, integration, and E2E test suites with mock Firestore layers

## Documentation
- [Master Technical Documentation](MASTER_DOCUMENTATION.md) – Fully consolidated system specifications, schemas, security remediations, and guides
- [Walkthrough & Deployment Guide](walkthrough.md) – Step-by-step setup, OAuth configuration, and verification notes
- [Security Review Report](SECURITY-REVIEW.md) – Log of remediated high-confidence vulnerabilities
- [System Handoff Report](handoff.md) – Production transition checklist and feature outcomes
