# ROADMAP

## HANDOFF, read this first

Written for a session with no memory of the one that wrote it. Everything below is
measured, not remembered: re-run `.venv\Scripts\python.exe code/evaluation/main.py` to
reproduce the scores.

**Deadline 2026-09-13T18:00:00+05:30. Compute the time left yourself before planning
anything, this file does not know when you are reading it.** AGENTS.md section 3 requires it
at session start anyway. The hard rule: **stop new work and package the moment 2 hours
remain**, whatever is in progress. A working pipeline that gets submitted beats a better one
that does not.

### Score, fresh run against the 25 solved rows

| Column | Score | State |
|---|---:|---|
| `amount_safe_to_pay` | 8.0% | live, and close to its ceiling (see D7) |
| `affordability_status` | 64.0% | live |
| `recommended_payment_method` | 68.0% | live |
| `payment_plan` | 64.0% | live |
| `earliest_date_for_full_payment` | 52.0% | live |
| `spending_changes_needed` | 88.0% | **placeholder**, constant `none` |
| **Overall** | **57.3%** | |

`decision_explanation` is 100% non-empty and is not scored for correctness.

### What is finished, what is not

- **Finished:** deterministic financial core (state reconstruction, 90-day forecast, monthly
  income recurrence), the evidence layer over 16 images and 215 messages, the eligibility
  filter and plan ranking, the affordability classifier, and the `payment_plan` grammar.
- **Placeholder:** `spending_changes_needed` returns a constant `none`. It scores 88% because
  22 of 25 ground-truth rows genuinely are `none`. Real headroom is 12 points on one column.
- **Known-broken:** nothing is broken. 29 tests pass, all 8 submission gates pass, the run is
  deterministic, and `output.csv` is byte-identical across runs.
- **Known-limited:** `image_04` is genuinely unreadable to the vision model and is left blank,
  never zeroed. That is correct behaviour, not a bug.

### The single highest-value next block

**`recommended_payment_method`, currently 68%.** It is worth roughly five times the
alternative because it pays twice: of the 9 rows where `payment_plan` is wrong, **8 are wrong
only because the method is wrong**, and the plan rendered is the correct plan for the method
chosen. Fixing the 8 method errors moves two columns at once, up to about 10 points overall.
Compare `spending_changes_needed`, the only remaining placeholder, which is capped at about 2
points overall because the constant `none` is already right 22 times in 25.

Start in `code/planning.py::classify`, not in the forecast. D11 established that the forecast
error moved into the classifier: `request_08`, `_11`, `_21` and `_22` are still misclassified
while their troughs sit within 0.03, 0.03, 0.09 and 0.00 months of the organizers' own. Their
arithmetic is right and their answer is wrong, so the remaining error is decision logic.

**Do not spend more time on `amount_safe_to_pay`.** D7 tested the one hypothesis for closing
it and falsified it. 21 of its 25 ground-truth values are arbitrary continuous numbers that
require reproducing the organizers' arithmetic to the unit.

### Open questions and unexplained rows

- **`request_05` is unexplained and should stay that way until someone measures it.** Its
  trough falls early, at day 7, yet it is out by +2.26 months of recurring spend: we project a
  dip of about 4,000 where the organizers project about 32,600, with no pending or scheduled
  rows involved and three salary credits projected inside the horizon. It does not fit the
  late-trough story that explained every other large error. No mechanism has been established.
  An honest unexplained row is better interview material than an invented cause.
- **Why the organizers do not apply a stated rent increase.** Four messages say rent rises 12%.
  Applying it measurably degrades four columns (D12), so we parse it, report it, and do not
  apply it. Why their forecast ignores a fact their own dataset states is unresolved.
- **`request_19` picks installments where the truth is `partial_payment`**, the only
  `partial_payment` row in the sample. Cost ranking says the split is cheaper; something in
  the eligibility or ranking path disagrees.

### Traps already hit and solved. Do not rediscover these.

1. **Free tier is 20 requests per day per model, and it is per model.** The last extraction run
   spent 13 successful calls (9 on `gemini-3.6-flash`, 4 on `gemini-3.7-flash`) plus some
   failed attempts that also consume quota; the configured head `gemini-3.8-flash` was already
   exhausted and the run fell down the chain. **You do not need any of it.** All 231 documents
   are in `.cache/` keyed by content hash, so a full run makes **zero live calls**. Do not
   re-run extraction to "refresh" anything: it costs quota, and it would change the token
   figures in `evaluation/usage_report.md`, which must describe the graded run.
2. **A failure that looks like a quota failure may not be one.** After batching landed, every
   batch came back empty and "quota again" was the obvious reading. It was wrong: a 45-second
   request timeout, tuned when a call was one small document, was cutting off 25-document
   batches that were working, and each timeout triggered a retry that spent more quota. A
   timeout tuned for one call shape is wrong for another. Read the actual error before
   accepting the story that fits the most recent incident.
3. **`code.zip` is built from `git ls-files`, so an untracked source file is silently omitted,
   not rejected.** The first dress rehearsal shipped an archive missing a test file written
   minutes earlier, with nothing in the output to say so. A gate now fails the build on any
   untracked file under `code/`. **Commit before packaging.**
4. **Non-ASCII in source crashes on a cp1252 console.** A stray CJK glyph in a
   `diagnose_trough.py` header string made the diagnostic die with a `UnicodeEncodeError` that
   looked like a data problem. Keep source ASCII.
5. **Two output columns format money differently.** `payment_plan` writes 996.6 as `996.60`;
   `amount_safe_to_pay` strips trailing zeros in the same file (`17229139.2`, `603.3`).
   Assuming they match costs four rows.
6. **Never push `upstream`.** It is the organizers' remote and is `no_push`. `origin` is the
   private backup and is safe.

### Before you submit

```
.venv\Scripts\python.exe code/package_submission.py --check   # 8 gates
.venv\Scripts\python.exe code/package_submission.py           # builds code.zip
.venv\Scripts\python.exe code/log_entry.py verify             # transcript leak scan
```

Submit `code.zip`, `output.csv`, and `log.txt` as the chat transcript, to
https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission

`log.txt` is gitignored but **is** submitted. Never edit it by hand; append only through
`code/log_entry.py`, which applies redaction and the required LF endings.

---

Buy or Wait?, HackerRank Orchestrate. Deadline: 2026-09-13T18:00:00+05:30 (AGENTS.md section 3).

Tasks and future ideas live here, never in README.md.

## 1. Score history

How the columns moved, oldest first. **Current numbers are in the HANDOFF above; this
table is the progression, kept as evidence of what each step actually bought.**

Verified by running `python code/evaluation/main.py` against the 25 solved rows in
`dataset/sample_requests.csv`.

| Column | Skeleton | Forecast | Forecast + evidence | Status |
|---|---|---|---|---|
| amount_safe_to_pay | 0.0% | 4.0% | **4.0%** | live, unchanged by evidence |
| affordability_status | 28.0% | 28.0% | **28.0%** | placeholder |
| recommended_payment_method | 28.0% | 28.0% | **28.0%** | placeholder |
| payment_plan | 28.0% | 28.0% | **28.0%** | placeholder |
| earliest_date_for_full_payment | 28.0% | 32.0% | **36.0%** | live, +4.0 from evidence |
| spending_changes_needed | 88.0% | 88.0% | **88.0%** | placeholder |
| **Overall (6 scored columns)** | **33.3%** | **34.7%** | **35.3%** | |

`decision_explanation` is 100% non-empty and not scored for correctness.

The evidence layer is built and applied: 215 of 215 messages classified, 15 of 16 blank amounts
resolved from their linked images. The 16th (`image_04`) was returned as unreadable by the vision
model and is correctly left blank rather than zeroed.

### The evidence-layer experiment failed, and that is the useful finding

The error on `amount_safe_to_pay` was 12 rows too high, 12 too low, 1 exact. After applying every
message and image it is **still 12 too high, 12 too low, 1 exact**. The hypothesis that the
symmetric error came from ignored message evidence is falsified. Full result, including the check
that the layer is genuinely connected (it moves 4 rows, improving 2 and worsening 2), is in
DECISIONS.md entry D7.

Consequence for planning: stop investing in `amount_safe_to_pay`. Exact match on it requires
reproducing the organizers' arithmetic to the unit across 21 arbitrary continuous values, and the
one hypothesis for closing that gap has been tested and rejected. The discrete columns are where
the remaining CSV points are.

What is still placeholder: `affordability_status`, `recommended_payment_method`, `payment_plan`,
`spending_changes_needed`. No eligibility filter, no plan ranking, no spending-change selection.

## 2. Remaining steps, revised order

Reordered on 2026-09-12 after measuring the error split on `amount_safe_to_pay`: 12 rows too
high, 12 too low, 1 exact. Symmetric error cannot come from a single missing expense rule, and
the evidence layer is the only input that can push a forecast in both directions. It therefore
moved ahead of eligibility and ranking rather than sitting at the end.

That experiment has now run and the hypothesis was falsified: the split is unchanged. Step 1 is
complete and the evidence layer ships, but it is not the fix for that column. Reasoning in
DECISIONS.md D2, result in D7.

| # | Step | Produces | Moves | Est. | Blocked by |
|---|---|---|---|---|---|
| 1 | DONE. Evidence layer: vision over the 16 linked images, text classification over the 215 messages, both cached by content hash, applied through the conflict-resolution order | resolved blank amounts, amended/cancelled/delayed facts | amount_safe_to_pay, earliest_date_for_full_payment | 4h | none |
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

This section carried a fixed hour-by-hour plan written on 2026-09-12. It went stale
within a day, which is the argument against writing one down at all. Budget against the
deadline you compute at session start instead.

What is left is small and can be ordered by value rather than by clock:

| Block | Rough cost | Worth |
|---|---|---|
| `recommended_payment_method` in `classify` | the bulk of a session | up to ~10 points, moves `payment_plan` with it |
| `spending_changes_needed`, the last placeholder | less | capped at ~2 points overall |
| Packaging and submitting | under 30 minutes, already rehearsed end to end | everything |

Rule, unchanged and the only one that matters: **stop new work and package the moment 2
hours remain**, whatever is in progress. Partial credit on a working pipeline beats a better
pipeline that never gets zipped. The packaging path has been run for real (D14), so it is
minutes, not an unknown.

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
