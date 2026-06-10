SECURITY REVIEW — bodie-tours
Date: 2026-06-09

Summary

This document records high-confidence security findings from automated security reviews and code scans. It consolidates open issues that require developer attention and proposed remediation steps.

Findings (short)

| # | Severity | File | Vulnerability | Status |
|---|----------|------|---------------|--------|
| 1 | 🔴 HIGH     | /home/freya/bodie-tours/main.py | HTML Injection in M365 Calendar Event (unescaped user input) | OPEN |
| 2 | 🔴 HIGH     | /home/freya/bodie-tours/main.py | HTML Injection in Customer-facing Emails (unescaped placeholders) | OPEN |
| 3 | 🔴 HIGH     | /home/freya/bodie-tours/prune_unpaid_slots.py | HTML Injection in Reminder Emails (unescaped placeholders / custom templates) | OPEN |
| 4 | 🔴 HIGH     | /home/freya/bodie-tours/main.py | Sensitive provider response bodies included in exceptions/logs (`response.text`) | OPEN |
| 5 | 🔴 HIGH     | /home/freya/bodie-tours/main.py | OAuth redirect_uri sourced from Firestore/config without strict validation | OPEN |
| 6 | 🔴 HIGH     | /home/freya/bodie-tours/deploy_functions.sh | Booking endpoint deployed as publicly unauthenticated (abuse/automation risk) | OPEN |
| 7 | 🟠 MEDIUM   | /home/freya/bodie-tours/main.py | Overly Permissive CORS Origin Handling (origin echoing/wildcarding) | OPEN |

Details, Remediations & Status

1) HTML Injection in M365 event (main.py)
- Why: User-supplied guest fields are used directly to build HTML event body/subject without escaping.
- Risk: Phishing, malicious links, or client-side rendering issues in calendar/email clients.
- Remediation: Escape all untrusted values with html.escape() before inserting into HTML. Prefer sending text-only content or using structured event fields. Validate/whitelist guest fields (names, phone formats) and cap lengths.
- Suggested fix: Apply html.escape to guest fields when composing event_payload. Add unit tests that assert sent payloads do not contain raw tags from attacker-controlled inputs.
- Status: OPEN

2) HTML Injection in customer emails (main.py)
- Why: Email templates (custom or fallback) are populated with user-controlled values using naive string replacement without escaping.
- Risk: Malicious links or spoofed content in emails to customers or staff.
- Remediation: Use html.escape() for placeholder values; restrict or sanitize custom templates stored in Firestore; consider using a templating engine with auto-escaping or send plain-text emails.
- Suggested fix: Replace template replacement loop with escaped values and validate URLs included in templates.
- Status: OPEN

3) HTML Injection in reminder emails (prune_unpaid_slots.py)
- Why: send_outlook_reminder loads templates (possibly from Firestore) and replaces placeholders with customer-supplied data without escaping.
- Risk: Reminder emails may contain injected HTML or links used for phishing.
- Remediation: Sanitize template inputs and escape placeholder values (html.escape). Add tests for custom-template rendering.
- Status: OPEN

4) Sensitive provider response bodies included in exceptions/logs (main.py & prune_unpaid_slots.py)
- Why: Code raises Exceptions or logs using response.text from OAuth/token endpoints and provider APIs.
- Risk: Error bodies can include sensitive debug info or tokens and may be written to logs or propagated to clients.
- Remediation: Do not embed raw response bodies in exceptions. Log generic messages and, if needed, store redacted/truncated snippets in protected logs. Use response.raise_for_status() and handle errors with sanitized messages.
- Suggested fix: Replace raised Exception(... response.text ...) with generic message and log a redacted snippet (e.g., response.text[:200]) to protected logs.
- Status: OPEN

5) OAuth redirect_uri sourced from Firestore/config without strict validation (main.py)
- Why: redirect_uri used in OAuth flows can come from an editable Firestore document or environment and is not strictly validated.
- Risk: If an attacker can modify Firestore or deployment config, they could set redirect_uri to an attacker-controlled endpoint and capture authorization codes/tokens.
- Remediation: Only allow redirect_uri from a pre-approved whitelist stored in immutable configuration (environment/secret manager). If config is editable, enforce an exact-match whitelist and audit changes.
- Suggested fix: Add validation against an env-configured allowed redirect_uri list and fail-fast if not matched.
- Status: OPEN

6) Booking endpoint deployed as publicly unauthenticated (deploy_functions.sh)
- Why: deploy_functions.sh deploys handle-booking and certain other functions with --allow-unauthenticated by default.
- Risk: Attackers can programmatically create bookings, invoices, and calendar events, leading to abuse and resource exhaustion.
- Remediation: Require authentication for endpoints that create invoices or send emails. If public access is required, implement server-side anti-abuse controls (rate-limiting, CAPTCHA, quotas) and monitoring/alerts.
- Suggested fix: Remove --allow-unauthenticated for production deploys. Use IAM or OIDC authentication, or gate creation behind signed requests from the frontend.
- Status: OPEN

7) Overly Permissive CORS (main.py)
- Why: The code echoes Origin for many origins (suffix matches and substring matches), allowing broader cross-origin access.
- Risk: Cross-origin attacks from attacker-controlled subdomains or local origins, potential for CSRF or data exfiltration from browsers.
- Remediation: Use an exact-match whitelist configured via environment variables. Do not echo arbitrary Origin headers in production.
- Suggested fix: Replace suffix/substring checks with exact-match lookup in an environment-provided list; enable localhost allowances only under a dev flag.
- Status: OPEN

Recommended prioritization
1. Immediate (HIGH): Fix provider response logging (4), ensure redirect_uri validation (5), remove unauthenticated deployment for booking (6).
2. Immediate (HIGH): Escape/sanitize all HTML-producing email/calendar templates (1,2,3).
3. Short-term (MEDIUM): Harden CORS handling (7) and add tests for sanitization and template rendering.

Repository artifacts
- Security review file updated and saved at: /home/freya/bodie-tours/SECURITY-REVIEW.md

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
