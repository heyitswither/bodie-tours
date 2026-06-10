SECURITY REVIEW — bodie-tours
Date: 2026-06-09

Summary

This document records high-confidence security findings from an automated security review focused on production-impact issues (HTML injection in email/calendar content, CORS, and logging of provider responses). Findings are actionable with minimal patches.

Findings (short)

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🔴 HIGH     | /home/freya/bodie-tours/main.py | 214-263 | HTML Injection in M365 Calendar Event (unescaped user input) | 8/10 |
| 2 | 🔴 HIGH     | /home/freya/bodie-tours/main.py | 266-406 | HTML Injection in Customer-facing Emails (unescaped template placeholders) | 8/10 |
| 3 | 🔴 HIGH     | /home/freya/bodie-tours/prune_unpaid_slots.py | 101-170 | HTML Injection in Reminder Emails (unescaped template placeholders) | 8/10 |
| 4 | 🟠 MEDIUM   | /home/freya/bodie-tours/main.py | 672-705 | Overly Permissive CORS Origin Handling in handle_booking (origin echoing / suffix matches) | 8/10 |
| 5 | 🟠 MEDIUM   | /home/freya/bodie-tours/main.py | 115-117 | Sensitive provider response bodies logged/raised (response.text) | 8/10 |

Details & Remediations

1) HTML Injection in M365 event (main.py:214-263)
- Why: User-supplied guest fields are inserted into HTML event bodies/subjects without escaping.
- Risk: Phishing, malicious links, or client-side rendering issues in calendar/email clients.
- Fix: Escape values with html.escape() or send plain-text; validate/whitelist guest input. See suggested patch in review notes.

2) HTML Injection in customer emails (main.py:266-406)
- Why: Template replacement inserts user values into HTML without escaping.
- Risk: Phishing links or spoofed content to customers/staff.
- Fix: Use html.escape() on all placeholder values before replacement or send text-only emails.

3) HTML Injection in reminder emails (prune_unpaid_slots.py:101-170)
- Why & Risk: Same as (2) for reminders; custom templates from Firestore are vulnerable.
- Fix: Escape placeholder values; sanitize custom templates before use.

4) Overly permissive CORS (main.py:672-705)
- Why: Origin echoing allows *.squarespace.com and substring matches for localhost, increasing attack surface.
- Risk: Cross-origin attacks from attacker-controlled subdomains.
- Fix: Enforce exact-match whitelist from configuration; do not echo user-provided Origin.

5) Sensitive response.text logging (main.py:115-117)
- Why: Exceptions include raw provider response bodies.
- Risk: Logs could contain sensitive info; avoid printing full response bodies.
- Fix: Replace response.text in exceptions with generic messages; log truncated/redacted snippets only in secured logs.

Suggested next steps
- Apply minimal patches to escape HTML in email/event code paths (high priority).
- Harden CORS to exact whitelist from env/config.
- Replace provider response text in raised exceptions with sanitized messages.
- Run tests and live verification (already performed) and add unit tests asserting escaping behavior.

Repository artifacts
- Security review generated and saved at: /home/freya/bodie-tours/SECURITY-REVIEW.md

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
