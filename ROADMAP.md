# ROADMAP

Buy or Wait?, HackerRank Orchestrate. Deadline: 2026-09-13T18:00:00+05:30 (AGENTS.md section 3).
As of this writing: 2026-09-12 ~20:30 IST, about 21.5 hours left.

Tasks and future ideas live here, never in README.md.

## 1. Current state

Verified by running `python code/evaluation/main.py` against the 25 solved rows in
`dataset/sample_requests.csv`.

Baseline before this session's work (walking skeleton, every column a safe placeholder):

| Column | Score |
|---|---|
| amount_safe_to_pay | 0.0% |
| affordability_status | 28.0% |
| recommended_payment_method | 28.0% |
| payment_plan | 28.0% |
| earliest_date_for_full_payment | 28.0% |
| spending_changes_needed | 88.0% |
| decision_explanation | 100.0% non-empty (not scored for correctness) |
| **Overall (6 scored columns)** | **33.3%** |

After this session (`code/financial_state.py`, the deterministic 90-day forecast, wired into
`code/main.py` for two columns only):

| Column | Score | Change |
|---|---|---|
| amount_safe_to_pay | 4.0% | moved off 0%, as expected |
| affordability_status | 28.0% | unchanged, still placeholder |
| recommended_payment_method | 28.0% | unchanged, still placeholder |
| payment_plan | 28.0% | unchanged, still placeholder |
| earliest_date_for_full_payment | 32.0% | moved, as expected |
| spending_changes_needed | 88.0% | unchanged, still placeholder |
| **Overall (6 scored columns)** | **34.7%** | +1.4 points |

The exact-match rate on the two live columns is low (4%, 32%) even though the model is now
structurally sound. Two real bugs were found and fixed by direct data inspection during the
build, not by tuning against the sample answers:

1. Recurring income was not detected at all. Fixed by adding the same cadence-consistency
   detector to settled income, keeping description in the grouping key so genuine confirmed
   salary (one employer, identical description every month, verified on `user_02`) is trusted
   but rotating gig income (multiple named platforms under one `salary` category, verified on
   `user_10`) is not.
2. Recurring expenses were grouped by `(category, description)`, so any essential category
   billed under a different description each time (groceries, transport, dining, verified
   missing entirely for `user_10`) never reached the occurrence threshold and was invisible
   to the forecast. Fixed by grouping debits by category alone.

Fixing bug 2 made `user_10`'s numbers much more sensible (earliest date now correctly empty,
matching the sample) but made `user_02`'s exact-match slightly worse (a more complete forecast
pushed its earliest safe date later than the sample's). Reported here rather than reverted,
per the standing rule not to tune quietly toward a better-looking number. The remaining gap is
most likely the conservative-amount heuristic (max/min of the last 3 occurrences) not matching
whatever formula produced the ground truth exactly. Candidate refinement, not committed to yet:
see the amount-precision task below.

What is still placeholder: `affordability_status`, `recommended_payment_method`, `payment_plan`,
`spending_changes_needed`. No eligibility filter, no plan ranking, no spending-change selection,
no LLM calls, no usage report content.

## 2. Remaining steps, revised order

Reordered on 2026-09-12 after measuring the error split on `amount_safe_to_pay`: 12 rows too
high, 12 too low, 1 exact. Symmetric error cannot come from a single missing expense rule, and
the evidence layer is the only input that can push a forecast in both directions. It therefore
moves ahead of eligibility and ranking rather than sitting at the end. Full reasoning and the
falsification criterion are in DECISIONS.md, entry D2.

| # | Step | Produces | Moves | Est. | Blocked by |
|---|---|---|---|---|---|
| 1 | Evidence layer: vision over the 16 linked images, text classification over the 215 messages, both cached by content hash, applied through the conflict-resolution order | resolved blank amounts, amended/cancelled/delayed facts | amount_safe_to_pay, earliest_date_for_full_payment | 4h | none |
| 2 | Eligibility filter and plan ranking: match payment preferences, `max_installment_months` and `allows_partial_payment` against `request_payment_options.csv`, then apply the 6-tier ranking | chosen plan | affordability_status, recommended_payment_method | 3.5h | Step 1 |
| 3 | Payment plan emission and spending-change selection: the `YYYY-MM-DD:amount` grammar, and up to 3 stop/reduce actions respecting protected categories and `minimum_allowed_amount` | plan string, change list | payment_plan, spending_changes_needed | 2.5h | Step 2 |
| 4 | Unit tests, ARCHITECTURE.md, code/README.md | code-zip quality evidence | code zip score (30 pts) | 2h | Step 3 |
| 5 | Cost tracking rollup and `evaluation/usage_report.md` from the measured run | required deliverable | submission gate | 1h | Step 1 |
| 6 | DECISIONS.md completion and judge-interview prep | interview answers | interview score (30 pts) | 1.5h | Steps 1-5 |
| 7 | Full 250-row run, contract validation, code.zip build | output.csv, code.zip | submission gates | 1h | Steps 3, 5 |
| 8 | Rest block | - | - | 1.5h | - |
| 9 | Buffer | - | - | 2h | - |

Total estimated work: 19h against 21h remaining.

### Why this order and not the score-greedy one

The challenge is scored out of 100: chat transcript 10, judge interview 30, output CSV 30, code
zip 30. Seventy of those points are not the CSV. Steps 4 and 6 are therefore not housekeeping to
be done if time allows; they carry as many points as every output column combined, and they are
far less contested than the CSV, where the live leaderboard tops out near 50.

Within the CSV's own 30 points, `amount_safe_to_pay` is the lowest-yield column: 21 of the 25
ground-truth values are arbitrary continuous numbers and none are zero, so scoring it means
reproducing the organizers' arithmetic to the unit. `affordability_status` and
`recommended_payment_method` are close to a single column (only 6 distinct pairs across 25 rows,
and three of those pairs are one-to-one), so step 2 buys two columns for one piece of logic.
Step 1 comes first regardless, because every later column reads the forecast it corrects.

## 3. Time budget

| Block | Hours |
|---|---|
| Remaining as of 2026-09-12 20:51 IST | 21.1h |
| Steps 1-7 above (build) | 15.5h |
| Step 8, rest block | 1.5h |
| Step 9, buffer | 2.0h |
| Submit floor, packaging already counted in step 7 | 1.0h |
| Total planned | 20.0h, fits inside 21.1h with 1.1h slack |

Rule: stop new work and start Step 7 (packaging) the moment 2 hours remain before the
deadline, whatever step is in progress. Partial credit on a working pipeline beats a better
pipeline that never gets zipped.

## 4. Submission deliverables

| Deliverable | Status | Still needed |
|---|---|---|
| code.zip | buildable now (`package_submission.py`), gates pass | steps 1-11 to make the shipped logic complete, then rebuild |
| output.csv | present, 250 rows, correct columns, correct order | real predictions for 4 of 8 columns still placeholder |
| log.txt (chat_transcript) | append-only, passes `log_entry.py verify` | keep appending every turn through submission; do not hand-edit |

## 5. Risks

| Risk | Cheapest mitigation |
|---|---|
| Plan ranking (6 tie-break levels) has a subtle bug | Unit-test against the 25 sample rows before touching the LLM steps, since they are the only labeled ground truth available |
| Gemini rate limits or slow responses during the 16-image / 215-message pass | Cache every response to disk by content hash immediately; a rerun costs nothing and never re-spends tokens |
| Vision misreads a blank amount | Log the raw model output next to the parsed number in the cache file so a bad read is visible and fixable, not silently trusted |
| Running low on time before eligibility/ranking lands | The step-12 buffer and the 2-hour stop rule exist for this; output.csv already has a valid placeholder fallback per row, so a partial run degrades safely instead of breaking the contract |
| Conservative-amount heuristic still misses exact sample answers | Tracked as a known gap (section 1), not a blocker; the six-column scorer is the feedback loop to close it if time allows after step 8 |
| Accidentally committing dataset/, .env, or log.txt | `package_submission.py --check` gate 1 catches this before every build; run it before every commit that touches packaging |
