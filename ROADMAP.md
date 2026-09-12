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

## 2. Remaining steps, in dependency order

| # | Step | Produces | Moves | Est. | Blocked by |
|---|---|---|---|---|---|
| 1 | Eligibility filter: match `payment_methods_user_will_consider`, `max_installment_months`, `allows_partial_payment` against `request_payment_options.csv` | list of safe, eligible plans per request | affordability_status, recommended_payment_method | 1.5h | financial_state.py (done) |
| 2 | Plan ranking: apply the 6-tier order (complete by deadline, no spending changes, minimize total paid, start earlier, fewer payments, lowest payment_option_id) | chosen plan | recommended_payment_method, payment_plan, affordability_status | 2.5h | Step 1 |
| 3 | Spending-changes selection: pick up to 3 stop/reduce actions from willing-to-reduce/stop categories, respecting protected categories and minimum_allowed_amount, mutually exclusive stop vs reduce per event | spending change list | spending_changes_needed, affordability_with_plan cases | 1.5h | Step 2 |
| 4 | Wire 1-3 into main.py, derive decision_explanation from the real inputs (grounded, short, cites real events/dates) | full non-LLM pipeline | decision_explanation quality | 1h | Steps 1-3 |
| 5 | Re-run self-scoring harness, debug against the 25 samples | updated score table | all 6 columns | 1.5h | Step 4 |
| 6 | Blank-amount resolution: Gemini vision over the 16 linked images, cached to disk by content hash | resolved amounts, incomplete flag cleared | amount_safe_to_pay, earliest_date_for_full_payment for ~16 affected users | 2h | none (independent of 1-5) |
| 7 | Message extraction: Gemini text over 215 messages, apply the conflict-resolution order (cancellation/settlement/amendment, then newer same-source record, then settled over estimate, then safer interpretation) | amended/clarified facts | amount_safe_to_pay, earliest_date_for_full_payment, decision_explanation | 2.5h | none (independent of 1-5) |
| 8 | Integrate steps 6-7 outputs back into financial_state, re-run self-scoring, debug | updated score table | all 6 columns, again | 1.5h | Steps 5, 6, 7 |
| 9 | Token/cost tracking on both LLM call sites, write evaluation/usage_report.md from the real run | required deliverable | none directly, submission gate | 1h | Steps 6, 7 |
| 10 | Full 250-row run to output.csv, validate contract (`package_submission.py --check`) | output.csv | submission gate | 0.5h | Step 8 |
| 11 | README run instructions, final code.zip build, submission gates | code.zip | submission gate | 1h | Step 10 |
| 12 | Buffer for the unknowns steps 1-11 will surface | - | - | 2h | - |

Total estimated work: 18.5h.

## 3. Time budget

| Block | Hours | Runs until (IST, approx) |
|---|---|---|
| Remaining now | 21.5h | - |
| Steps 1-12 above | 18.5h | - |
| Rest block | 1.5h | - |
| Submit buffer (floor) | 1.0h | - |
| **Total planned** | **21.0h** | fits inside 21.5h with 0.5h slack |

Rule: stop new work and start Step 11 (packaging) the moment 2 hours remain before the
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
