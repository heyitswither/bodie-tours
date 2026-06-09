# E2E Test Infra: Bodie Tours Booking System

## Test Philosophy
- Opaque-box, requirement-driven. No dependency on implementation design.
- Methodology: Category-Partition + BVA + Pairwise + Workload Testing.

## Feature Inventory
| # | Feature | Source (requirement) | Tier 1 | Tier 2 | Tier 3 |
|---|---------|---------------------|:------:|:------:|:------:|
| 1 | QBO OAuth Flow & Refresh Token | ORIGINAL_REQUEST §R1 | 5      | 5      | ✓      |
| 2 | QBO Invoice Generation & Link | ORIGINAL_REQUEST §R1 | 5      | 5      | ✓      |
| 3 | M365 Availability Check | ORIGINAL_REQUEST §R2 | 5      | 5      | ✓      |
| 4 | M365 Calendar Event Injection | ORIGINAL_REQUEST §R2 | 5      | 5      | ✓      |
| 5 | Pruning: Dynamic TTL Calc | ORIGINAL_REQUEST §R3 | 5      | 5      | ✓      |
| 6 | Pruning: Reminder Email | ORIGINAL_REQUEST §R3 | 5      | 5      | ✓      |
| 7 | Pruning: Expiration Cancellation | ORIGINAL_REQUEST §R3 | 5      | 5      | ✓      |
| 8 | Pruning: M365 Event Removal | ORIGINAL_REQUEST §R3 | 5      | 5      | ✓      |
| 9 | Pruning: Completed Tour Cleanup | ORIGINAL_REQUEST §R3 | 5      | 5      | ✓      |

## Test Architecture
- Test runner: `pytest`
- Invocation: `pytest tests/e2e/ -v`
- Pass/Fail Semantics: 0 exit code indicates success
- Directory layout:
  - `tests/e2e/` (root for E2E tests)
  - `tests/e2e/tier1_feature/`
  - `tests/e2e/tier2_boundary/`
  - `tests/e2e/tier3_cross_feature/`
  - `tests/e2e/tier4_real_world/`
- Mocking Strategy: Since this is an E2E test without deploying real external infrastructure (like actual QBO/M365 accounts), the E2E tests will run against the HTTP functions/entry points using `flask` or `functions-framework` test clients, while mocking the external API calls (QBO/M365/Firestore).

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | Normal successful booking, payment, and tour completion | 2, 3, 4, 9 | High |
| 2 | Booking made, but TTL expires before payment | 2, 3, 4, 5, 6, 7, 8 | High |
| 3 | Booking attempted but no M365 availability | 3 | Medium |
| 4 | QBO OAuth token expiration & refresh during booking | 1, 2, 3, 4 | High |
| 5 | Reminder email sent, then paid just before TTL | 2, 3, 4, 5, 6 | High |

## Coverage Thresholds
- Tier 1: ≥5 per feature
- Tier 2: ≥5 per feature (where boundaries exist)
- Tier 3: pairwise coverage of major feature interactions
- Tier 4: ≥5 realistic application scenarios
