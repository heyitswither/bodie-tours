# Bodie State Park Booking System

A secure, hardened, serverless backend integration and premium frontend widget designed to manage visitor tours for the **Bodie State Park**. This system connects Google Cloud Functions, Firestore, QuickBooks Online (QBO) for invoicing, and Microsoft 365 (M365) for scheduling and automated calendar coordination.

---

## 🌟 Key Features

- **Standardized Pacific Time & Scheduling**: Operates globally using the `America/Los_Angeles` timezone for all calendar math, email communication, and slot pruning.
- **Unified Inventory Control**: Uses a strict, transaction-safe Firestore array-based inventory scheme (`taken_slots`) to handle concurrent reservations and prevent overbooking.
- **Microsoft 365 Synchronization**: Dynamically inspects tour guide availability and schedules events directly into team calendars. Automates sending `.ics` invitation attachments in booking confirmations.
- **Automated QuickBooks Online Invoicing**: Instantly creates professional customer invoices, supports high-reliability checkout redirection, and includes double-submit CSRF and strict state token verification.
- **Intelligent TTL Pruning & Email Campaigns**: Cleans up abandoned/unpaid bookings after a configurable Time-To-Live (TTL). Delivers up to 2 customized payment reminder emails (with the second reminder dispatched exactly at the 75% elapsed quarter-TTL mark).
- **Hardened Security Architecture**: Remediated for HTML injection, exact-match CORS origins, sensitive API logging leaks, and token validation timezone discrepancies.
- **High-Performance Responsive Widget**: An embedding-friendly HTML5 booking wizard. Features fluid 2-column wide layout support, 2x2 contact forms, and duration-aware time range displays (e.g., `10:00 AM - 1:00 PM` for a 3-hour tour).

---

## 📂 Repository Structure

The codebase is structured to isolate front-end widgets, serverless cloud functions, setup tools, visual screenshots, and test suites.

### Core Source Files
- [booking_widget.html](booking_widget.html) – The primary client-facing frontend booking wizard. Implements responsive CSS container-queries, popup-blocker safe payment redirections, and multi-step workflows.
- [main.py](main.py) – Google Cloud Function for handling reservations, CORS preflights, webhook endpoints, and OAuth callback handshakes (QBO and M365).
- [prune_unpaid_slots.py](prune_unpaid_slots.py) – Scheduled background Google Cloud Function that enforces booking TTLs, emails reminders, and transactionally releases expired slots.
- [tours_config.py](tours_config.py) – Standard configuration definitions and pricing algorithms for the five official Bodie Foundation tours.
- [retry_unpaid_bookings.py](retry_unpaid_bookings.py) – Autonomous background job to identify invoice link errors and rebuild links.
- [seed_templates.py](seed_templates.py) – Automation script to compile and upload premium HTML receipt and reminder layouts to Firestore.
- [dev_server.py](dev_server.py) – Local development mock server simulating both GCP Cloud Functions and Firestore responses.

### Deployment & Operation Shell Scripts
- [deploy_project.sh](deploy_project.sh) – Automation script to provision resource structures, Firestore indexes, and GCP configurations.
- [deploy_functions.sh](deploy_functions.sh) – Standardized Cloud CLI commands to compile and push backend serverless scripts to GCP.
- [verify_integrations.py](verify_integrations.py) – Validation utility to check and test live API endpoints, secret credentials, and active tokens.

### Configuration & Rules
- `firebase.json` / `firestore.rules` – Standard rulesets defining security boundaries and indexing structures for the Firestore instances.
- `firestore.indexes.json` – Database composite indices definitions.
- `requirements.txt` – Pruned, minimal production library dependencies.
- `requirements-dev.txt` – Additional utilities required for testing (e.g., `pytest`).

### Comprehensive Developer Documentation
- [MASTER_DOCUMENTATION.md](MASTER_DOCUMENTATION.md) – Unified master manual containing system architecture, database design contracts, integrations, and deployment playbooks.
- [PROJECT.md](PROJECT.md) – Development milestones, database architecture schemas, and core interface contracts.
- [walkthrough.md](walkthrough.md) – Step-by-step deployment playbook for GCP, Firestore, Azure AD/Entra, and QuickBooks Developer setups.
- [handoff.md](handoff.md) – Product handoff notes, checklist completions, and codebase advancements.
- [SECURITY-REVIEW.md](SECURITY-REVIEW.md) – Detailed analysis of remediated web security vulnerabilities.
- [TEST_INFRA.md](TEST_INFRA.md) – Testing principles, integration suites layout, and coverage goals.

---

## 🛠️ Getting Started: Local Development & Verification

Follow these steps to run a fully functional local playground of the system:

### 1. Set Up Environment
Create a clean environment and install dependencies:
```bash
pip install -r requirements-dev.txt --break-system-packages
```

### 2. Run the Local Mock Server
Launch `dev_server.py` to emulate Firestore, QBO, and M365 APIs on ports 8080/8081:
```bash
python3 dev_server.py
```

### 3. Open the Widget
Open `booking_widget.html` directly in any browser, or run a simple local web server:
```bash
python3 -m http.server 8000
```
Then navigate to: [http://localhost:8000/booking_widget.html](http://localhost:8000/booking_widget.html)

### 4. Running Test Suites
Execute the comprehensive test suites (E2E, boundary, unit, and security):
```bash
pytest tests/ -v
```

---

## 🚀 Deployment Playbook (Summary)

For detailed, step-by-step instructions, refer to the [Walkthrough & Deployment Guide](walkthrough.md).

- **GCP Project & Database Setup**:
  - Register a project on the [GCP Console](https://console.cloud.google.com/).
  - Provision a Firestore database in Native Mode named `bodie-tours` (do **NOT** use `(default)`).
- **Provision Invoker Service Accounts**:
  - Create a service account with Cloud Functions Invoker rights for secure background triggers.
- **Deploy Backend Cloud Functions**:
  - Execute `deploy_functions.sh` to push functions `handle-booking` and `prune-unpaid-slots` to Google Cloud Run.
- **Seed Dynamic Email Templates**:
  - Run `python3 seed_templates.py` to seed high-end forest-green templates into Firestore.
- **Establish OAuth Credentials**:
  - Set up application access inside QuickBooks Developer and Microsoft Entra.
  - Enter callback URLs and save keys directly into the Firestore `config` collection.
- **Schedule Automated Cron Pruning**:
  - Configure a Google Cloud Scheduler job at `*/15 * * * *` calling the secured pruning endpoint.
- **Embed on Squarespace**:
  - Insert `booking_widget.html` into an HTML Code Block, updating the `API_BASE_URL` to point to the live GCP endpoint.

---

## 🔍 UI Scenarios & Visual Proof

Verified user behaviors and UI interactions have been documented with network traces, console logs, and visual screenshots in the [screenshots/](screenshots/) directory:

- **Scenario 1: Happy Path**
  - Flawless booking reservation and invoice redirection.
  - ![Happy Path Screenshot](screenshots/happy_path.png)
- **Scenario 2: Sold-out Slots**
  - Visual feedback and disabled clicks for fully booked slots.
  - ![Sold-out Slots Screenshot](screenshots/sold_out.png)
- **Scenario 3: Empty Month**
  - Graceful rendering of months lacking schedule availability.
  - ![Empty Month Screenshot](screenshots/empty_month.png)
- **Scenario 4: Input Validation Failures**
  - Interactive form warnings for invalid emails/group sizes.
  - ![Validation Failure Screenshot](screenshots/validation_failure.png)
- **Scenario 5: Backend Error Response**
  - Themed warning banners on API failures.
  - ![Backend Error Screenshot](screenshots/backend_error.png)
