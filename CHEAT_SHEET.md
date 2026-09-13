# Cheat sheet

Live-recall card. Numbers only. Full argument in `JUDGE_BRIEF.md`, provenance in `DECISIONS.md`.

## Scores, 25 solved rows (`code/evaluation/main.py`)

| Column | Score | % |
|---|---|---|
| `amount_safe_to_pay` | 2/25 | 8.0 |
| `affordability_status` | 16/25 | 64.0 |
| `recommended_payment_method` | 17/25 | 68.0 |
| `payment_plan` | 16/25 | 64.0 |
| `earliest_date_for_full_payment` | 13/25 | 52.0 |
| `spending_changes_needed` | 22/25 | 88.0 (constant `none`) |
| **Overall, 6 scored** | **86/150** | **57.3** |

`decision_explanation`: 25/25 non-empty, 25/25 grounded, 25/25 consistent with method, avg 107 chars.

## Cost and model (`code/evaluation/usage_report.md`)

| | |
|---|---|
| Documents | 231 (16 images, 215 messages) |
| Model calls | 13 (batched 25 msg / 4 img per call) |
| Tokens | 48,353 in + 29,498 out = 77,851 |
| Avg tokens / request | 311.4 across 250 |
| Total cost | $0.0883 |
| Cost / request | $0.000353 |
| Re-run cost | $0.0000, 0 of 231 live |
| `gemini-3.6-flash` | 152 docs |
| `gemini-3.7-flash` | 79 docs |
| `gemini-3.8-flash` (configured head) | **0 docs** |
| Free-tier cap | 20 requests/day/model |

## Error shape

| | |
|---|---|
| **Error split, CURRENT** (25 rows) | **14 too high, 9 too low, 2 exact, skew 20%** |
| Error split, D7 era (before/after evidence, both) | 12 / 12 / 1, skew 0%. **Do not quote as current** |
| **Trough error, CURRENT** (21 binding rows) | before day 30: n=19, median 0.04. Day 30+: n=2, median 0.13. **About 3 to 1** |
| Trough error, D9 era | factor of 22, 4 rows at horizon edge. **Pre-D11, stale** |
| Rows at horizon edge now | **1** of 21 binding |
| Explicit ledger rows reach | ~day 20; past that the trough is all our own projection |
| Worst rows project | 0 or 1 income events vs 39 to 60 expense events |
| Delta / monthly recurring spend | median +0.03, mean +0.17, range -0.24 to +2.26 |
| our_trough / their_trough | median 1.030, range 0.814 to 3.057 |
| Sign, binding rows | 14 over, 7 under |
| Deltas matching a single event | 0 of 21. Small multiple of one series: 1 of 21 |

## Overfitting guards

| | |
|---|---|
| Absolute constants deciding 0 of 275 rows | 6 of 7 (F1-F6) |
| `NEAR_MISS_COVERAGE = 0.95` exposure | **5 in band, 2 decide** |
| Four-word distinction | "in band, versus decisive" |
| Near-miss on 25 samples | **0** |
| Income: zero-income users | **58 of 275 before, 17 after** (21.1% to 6.2%) |
| The 17 | 11 variable non-monthly earners, 6 single income event |
| Series recovered by 2-occurrence threshold | 90 |
| Series excluded by cadence rule | 41 |
| Income cancellations from an employer | 99 of 101 |
| Refused fit: horizon 60 / 30 | exact matches 2 to 4, refused |
| Rows that flipped it | `request_01` (trough day 90), `request_12` (day 89) |
| Reverted rule (D17) | status 64.0 to 60.0, method 68.0 to 64.0, overall 57.3 to 56.0 |
| Validator | 15 hard rules (block) + heuristics (warn) |
| Warnings on shipped output | **24 total: H1 23, H2 0, H3 1** |

## Metamorphic (`code/tests/test_metamorphic.py`)

| | |
|---|---|
| Result | **7 of 7 pass** |
| Runtime | ~2 min 20 s, outside the blocking gates |
| Mutation test P1 | 500.0 absolute floor -> failed, `request_01` k=0.01 expected 39.7318 got 0.0 |
| Mutation test P7 | honour flagged facts -> failed, 67 violations |
| Proves | consistency across transformed inputs |
| Does NOT prove | accuracy; runs on the 25 samples, not the 250 |

## Gates and determinism

| | |
|---|---|
| Submission gates | 10 of 10 pass |
| Specification rules | 15, 0 violations |
| `output.csv` sha256 | `28a50da9...067f`, byte identical across two runs |
| Fast tests | 41 checks across 5 scripts |

## Five rows to quote

| Row | One line |
|---|---|
| `request_05` | **Unexplained.** Troughs day 7 yet out by +2.26 months; we project ~4,000 dip vs organizers ~32,600. Recorded open, not guessed. |
| `request_24` | **Caught and fixed.** `message_17`, a prize notice, was read as ending a salary and drove the row to -2.66 mo. The `INCOME_AUTHORITY` guard fixed it: now **+0.03 mo** (65,880 vs 64,420). Quote as a fix, not a live defect. |
| `request_12` | **Horizon edge.** Trough on day 89 (2026-07-03 off 2026-04-05); evidence removes 5,547 of income, killing a valid 3-payment plan. One of the two rows the refused horizon-60 fit would have "fixed". |
| `request_04` | **Revert casualty.** Ground truth earliest date is 4 days inside the deadline, our forecast is a month late, so the stricter D17 rule pushed a correct row to `not_affordable`. |
| `request_241` | **Defensible edge case.** `not_recommended` while 100% is safe today: user accepts only installments and partial, seller forbids partial, both installment options breach the minimum. The lone H3 warning. |

## One-liners

- Architecture: deterministic core, model confined to 16 images + 215 messages.
- Rejected: end-to-end LLM (breaks contract silently on ~12 of 250), LLM forecast (nothing to add, much to perturb).
- `contains_embedded_instruction`: observability, not a control. No instruction path exists for evidence to reach.
- Trace any row: `.venv\Scripts\python.exe code/evaluation/trace.py <request_id>`
