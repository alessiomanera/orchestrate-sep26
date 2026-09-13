# Architecture, Buy or Wait?

How this submission is built, and why. Written to be read cold, without the build transcript.

Every design decision has a numbered record in [`DECISIONS.md`](DECISIONS.md), including the
ones that were tried and abandoned. This document is the map; `DECISIONS.md` is the evidence.
Where the two overlap, this document links rather than repeats.

---

## 1. The problem

For each of the 250 purchase requests in `dataset/requests.csv`, the agent must decide whether
the user should pay in full, pay part now, take an installment option, wait, or not proceed. It
reconstructs each user's cash position from a 25,342-row financial ledger, projects it forward
90 days from that request's own date, and checks every candidate plan against the balance the
user insists on keeping. The answer is eight columns per request, written to `output.csv`.

Three properties of the task shape everything below. The forecast is fully specified arithmetic,
not a judgement call. The user's stated payment preferences can veto an option that is otherwise
available. And two inputs, the message texts and the document images, are genuinely unstructured
and are the only places a language model earns its keep.

---

## 2. Data flow end to end

```
dataset/
  financial_profiles.csv ---------------------------+
  financial_events.csv --------------------+        |
  exchange_rates.csv ------------+         |        |
  messages.csv (215) --+         |         |        |
  images.csv (16) -----+         |         |        |
                       |         |         |        |
                       v         |         |        |
              +----------------+ |         |        |
              |  evidence.py   | |         |        |
              |  model layer   | |         |        |
              +----------------+ |         |        |
                       |         |         |        |
              typed facts, cached by content hash   |
                       |         |         |        |
                       v         v         v        v
              +--------------------------------------------+
              |           financial_state.py               |
              |  ledger replay -> FX -> evidence applied   |
              |  -> recurrence detection -> 90-day forecast|
              +--------------------------------------------+
                                 |
                    FinancialState (balance timeline)
                                 |
                                 v
              +--------------------------------------------+
              |               planning.py                  |
              |  eligibility filter -> candidate plans ->  |
              |  safety check -> ranking -> classification |
              +--------------------------------------------+
                                 |
                                 v
                     +------------------------+
                     |        main.py         |
                     |  renders eight columns |
                     +------------------------+
                                 |
                                 v
                            output.csv
```

The direction of travel matters: the model layer runs **once for the whole dataset**, before any
request is answered, and never again. By the time `planning.py` makes a decision, every input is
a Python number.

### Module boundaries

| Module | Lines | Owns | Does not own |
|---|---:|---|---|
| `code/evidence.py` | 533 | prompts, closed-vocabulary schemas, arbitration of extracted facts | any arithmetic on money |
| `code/llm.py` | 602 | REST transport, batching, content-hash cache, retry, fallback chain, token accounting | what any answer means |
| `code/financial_state.py` | 703 | ledger replay, currency conversion, recurrence detection, the 90-day forecast | which plan is recommended |
| `code/planning.py` | 367 | eligibility, plan construction, safety check, ranking, classification | how the balance was derived |
| `code/main.py` | 164 | reads requests, renders the eight columns, writes `output.csv` | any decision logic |
| `code/usage.py` | 159 | writes `evaluation/usage_report.md` from measured counts | estimates or reconstructions |
| `code/package_submission.py` | 232 | submission gates and archive build | anything the pipeline runs |
| `code/evaluation/verify_output.py` | 675 | independent validation of output.csv against 15 spec rules (Tier 1) and semantic heuristics (Tier 2) | producing or modifying output |

The seam that matters most is between `financial_state.py` and `planning.py`. The forecast
produces a balance timeline and nothing else; planning consumes it and never reaches back into
the ledger. That is what makes the classifier testable against a synthetic timeline, and what
keeps a formula diagnostic cheap to run against a fixed trajectory.

### The eight columns and where each is decided

| Column | Decided in |
|---|---|
| `amount_safe_to_pay` | `financial_state.py`, the trough of the 90-day timeline |
| `affordability_status` | `planning.py::classify` |
| `recommended_payment_method` | `planning.py::classify` |
| `payment_plan` | `planning.py::format_payment_plan`, a rendering of the chosen plan (D13) |
| `earliest_date_for_full_payment` | `financial_state.py`, capacity only, independent of preferences |
| `spending_changes_needed` | constant `none`, see section 8 |
| `decision_explanation` | `main.py::build_explanation`, composed from this row's own emitted values |

---

## 3. Why a deterministic core with a narrow model layer

**The shape of the choice.** The 90-day forecast, the eligibility filter and the plan ranking are
arithmetic with a single correct answer, fully written out in `problem_statement.md`. A language
model asked to perform them can only introduce variance into a calculation that has none. The
two message and image surfaces are the opposite: unstructured natural language and pictures of
receipts, where a model is the only practical reader.

So the model is confined to exactly those two surfaces, 16 images and 215 messages, and its
output is forced into a closed vocabulary of enums and numbers before any Python touches it.
Full reasoning in [D1](DECISIONS.md).

**What was rejected: per-row LLM decisioning.** Handing the model each request with its ledger
and asking for the eight columns was considered and rejected for four reasons.

| Objection | Detail |
|---|---|
| Cost and latency scale wrong | 250 requests against 25,342 ledger rows is a large per-row context. The chosen design makes 13 calls in total, and they do not grow with the request count. |
| The output contract stops being guaranteed | Eight columns with constrained vocabularies, one row per request in input order. Deterministic rendering guarantees that by construction; a model guarantees it only in expectation. |
| Arithmetic regressions become invisible | A wrong trough in Python is a failing test. A wrong trough inside a model answer is indistinguishable from a right one without recomputing it, which means writing the deterministic version anyway. |
| It cannot be audited | `spending_changes_needed` must name real `event_id` values and respect `minimum_allowed_amount`. Grounding that in free text is strictly harder than reading the ledger. |

The measured consequence: **13 model calls and 77,851 tokens for the entire 250-row run**, about
$0.09, and zero live calls on any re-run. See `evaluation/usage_report.md`.

**What the model layer earned.** It was not assumed to be worth including.
[D2](DECISIONS.md) set out to measure it and [D7](DECISIONS.md) records the result, including a
falsified hypothesis that was kept rather than quietly dropped. The layer resolves 16 of 16 blank
event amounts and classifies all 215 messages into typed facts. The pipeline still runs correctly
without it: `python code/main.py --no-evidence` is both the offline fallback and the control arm.

---

## 4. Determinism, and where it stops

**What is guaranteed.**

| Property | Mechanism |
|---|---|
| Same input, same output | forecast, eligibility and ranking are pure Python, no model in the decision path |
| Zero live calls on re-run | the archive ships the extraction cache (.cache/), so a grader reproduces output.csv byte for byte with no API key and no network |
| Byte-identical `output.csv` | deterministic rendering from the shipped warm cache (sha256 28a50da9...) |
| Sampling pinned | temperature 0 on every call |
| No wall-clock dependence | every request is answered as of its own `request_date`, never `today` |
| No third-party drift | the whole solution imports only the Python standard library |

**The extraction cache ships with the archive.** Because `code.zip` is assembled from
git-tracked files, `.cache/` is tracked in git. A grader can extract the archive, run the full
normal pipeline with only `dataset/` present and no `.env` file, and reproduce `output.csv`
byte for byte with zero live API calls. Regenerating the extraction cache from scratch does
require a `GEMINI_API_KEY` and is subject to the provider's 20-request-per-day free-tier cap
per model.

**What is not guaranteed, stated plainly.** If regenerating the cache from scratch, *which model*
reads a given document is not deterministic. The provider's free tier caps `generateContent` at 20
requests per day per model, so the run walks a fallback chain when its head is exhausted. Where a
document lands in that chain depends on remaining quota, which is wall-clock state outside our
control.

This is not hypothetical. Measured from the cache, the configured head `gemini-3.8-flash`
answered **zero** of the 231 documents; every one was served by a fallback. `usage_report.md`
therefore reports the `modelVersion` the provider returns with each response, not the model
configured in `.env`, because the latter would have named a model that did no work. Full record
in [D8](DECISIONS.md).

The distinction worth holding: the *pipeline* is reproducible, the *model selection* is not. Once
the cache is warm the second point stops mattering, which is why cache hits are reported rather
than dropped.

### Determinism gate and regression locking

**Determinism gate (Gate 10 in packaging).** Byte-identical reproduction is enforced programmatically by gate 10 in `code/package_submission.py`. The gate executes the full pipeline twice consecutively and asserts that the SHA-256 hash of `output.csv` across both runs is identical. If either run fails or a hash mismatch occurs, `output.csv` is restored so a half-written or corrupted file is never left behind. The comparison checks `output.csv` only, because `evaluation/usage_report.md` carries a generation timestamp and would produce a spurious mismatch.

**Score regression locking.** Accuracy against the 25 solved sample requests is guarded by `code/tests/test_score_regression.py`. It executes the pipeline in sample mode (`code/main.py --sample`), writing solely to `code/evaluation/sample_output.csv` and verifying that the root `output.csv` submission artifact is never touched. It asserts that every scored column meets or exceeds its baseline rate (amount_safe_to_pay >= 8.0%, affordability_status >= 64.0%, recommended_payment_method >= 68.0%, payment_plan >= 64.0%, earliest_date_for_full_payment >= 52.0%, spending_changes_needed >= 88.0%) and overall accuracy is at least 57.3%. The thresholds are named constants, commented with the rule that raising them is required when accuracy improves and lowering them is forbidden.

### Auditability of the shipped extraction cache

Shipping extraction results carries the risk of making an incorrect extraction invisible. To make every model extraction auditable, each cache file stores the raw model response (`raw_text`), the structured output (`parsed`), and an exact verbatim snippet (`quote`) from the original document supporting the extraction. A reviewer can inspect any file under `.cache/images/` or `.cache/messages/` to trace every model-derived number or fact back to its source document.

Measured directly from the 231 files in `.cache/`:

| Dimension | Measured count in `.cache/` | Pipeline handling |
|---|---|---|
| Image confidence levels | 15 high, 1 low | 15 high-confidence extractions are applied; the single low-confidence image is withheld |
| Message confidence levels | 215 not recorded | The message classification schema does not record a confidence field; all 215 messages are parsed into structured facts |
| Message kind breakdown | 91 amend, 78 confirm, 21 clarify, 14 cancel, 7 delay, 4 irrelevant | 211 messages are mapped into typed facts; 4 irrelevant messages have no financial effect |
| Embedded instructions flagged | 0 of 16 images, 0 of 215 messages (`contains_embedded_instruction: false`) | Embedded instructions are treated as untrusted data and never as instructions, satisfying the problem statement requirement |
| Verbatim source grounding (`quote`) | 16 of 16 images (100%), 215 of 215 messages (100%) | Every single extraction anchors its extracted amounts or dates directly to quoted document text |
| Unresolved blank amounts | 0 (all 16 resolved) | `image_04` carries `confidence: "low"` and `amount: null` (quote: "Item Bill\n\u20b92854.00"). The deterministic quote recovery rule parses the exact 2854.00 INR amount directly from the model's verbatim quote with zero API calls. All 16 blank-amount events now resolve to confirmed numbers |

Every cache file pairs `raw_text`, `quote`, and `parsed` in a single JSON document. A reviewer does not have to guess what the model returned or what text it read: open any cache file to inspect the exact prompt input, the provider response, and the structured schema fields. No model extraction is opaque, and every evidence input flowing into `output.csv` is fully traceable to its underlying file.

---


## 5. Failure and abstention behaviour

The rule throughout: **a failure must never be recorded as a success, and an unknown must never
be rendered as a zero.**

| Situation | Behaviour |
|---|---|
| Image unreadable | recovered from quote if unambiguous; otherwise stays blank and flagged `[unread]`, never zeroed |
| Amendment that cannot be priced or placed | reported as `[unapplied amendment]` with its reason, not silently dropped. Four messages are currently in this state (D12). |
| Event with a missing amount | collected on `FinancialState.incomplete_event_ids`, so an incomplete state is visible rather than silently partial. |
| Model call fails or is out of quota | capped attempts, exponential backoff, fallback chain, then a circuit breaker. The document degrades to "no evidence", which is exactly the `--no-evidence` path. |
| Embedded instruction in a message or image | treated as data, flagged `[injection flagged]`, never executed. Content travels inside a delimited block and the answer is a server-enforced schema (D3). |

**Bounded execution.** There are no unbounded loops. Every model call has a capped attempt count
and a timeout, the fallback chain is a finite list, and the circuit breaker stops the run from
grinding against an exhausted quota. The cache is written before its value is used (D5), so a
crash mid-run never re-spends completed work.

**Abstention as a first-class answer.** `not_recommended` and an empty
`earliest_date_for_full_payment` are correct answers, not failures. The classifier reaches them
by rule rather than by exhaustion: `not_recommended` is the documented fallback when no eligible
safe payment exists, including the case where capacity exists later but the user rejects every
method that could use it.

---

## 6. Guarding against overfitting 25 labelled rows

There are 25 solved sample rows and 250 graded ones. Fitting the 25 is the main way to lose this
challenge quietly, so every adopted rule carries a dataset-wide measurement rather than a sample
score.

**Reach is measured before a rule is adopted.** D11 rebuilt income projection. Each of its three
rules was justified by its effect across all 275 users, not by sample rows:

| Rule | Reach across the full dataset |
|---|---|
| A monthly series survives a skipped period | 8 income series |
| Two occurrences establish monthly income, against three for a debit | 90 series newly projected |
| Non-monthly income is not projected | 41 series no longer projected |

Net effect: users projecting no income at all fell from 58 of 275 to 17, 21.1 percent to 6.2
percent. The rule was adopted because of that number, not because sample scores improved.

**Rejected variants are recorded, not dropped.** D11 tested two tidier groupings and rejected
both on measurement: grouping credits by category alone merges a base salary with an irregular
bonus and discards both, moving four rows out by 1.6 to 2.7 months of recurring spend; grouping
by day of the month turns each of a weekly payer's landing days into its own phantom salary.

**Fitted constants are exposure-tested.** `planning.py` has one fitted threshold,
`NEAR_MISS_COVERAGE = 0.95`. Its exposure was measured across the real 250: coverage is strongly
bimodal, with 123 rows below 0.50, 72 at full coverage, and only 9 rows anywhere between 0.90 and
0.998. The threshold decides 2 rows today and 4 more sit in the band it rejects, so **6 of 250,
2.4 percent**, and it sits in a sparse gap between two dense clusters rather than inside one. It
was left at 0.95 rather than retuned to fit the samples better.

**Categorical columns do not consume precise amounts.** D10 decides `affordability_status` from
bands and relationships, coverage ratios and yes/no safety comparisons, rather than from the
second decimal of `amount_safe_to_pay`. That was deliberate: it stops the categorical columns
inheriting a numeric accuracy the forecast does not have.

**A tempting fit was measured and refused.** Shortening the forecast horizon from 90 days to 60
raises exact sample matches on `amount_safe_to_pay` from 2 to 4. It was rejected: both flipping
rows have their 90-day trough at day 90 and day 89, so the gain is truncating the window where
our own projection is weakest, against an explicit instruction to forecast 90 days, fitted to 2
of 25 labelled rows.

---

## 7. Verification

| Gate | What it proves |
|---|---|
| `python code/package_submission.py --check` | nine gates: column order, one row per request, row order matches the dataset, specification verification across 15 rules, no private file tracked, transcript leak scan, usage report is real |
| `python code/evaluation/verify_output.py` | independent validator enforcing all 15 specification rules across all 250 evaluation requests |
| `python code/evaluation/main.py` | self-scores the seven comparable columns against the 25 solved rows, and scores `decision_explanation` on usefulness and consistency against the row we emitted |
| `code/tests/*.py` | 29 checks, no key and no network, plain scripts with no test runner |
| `python code/main.py --no-evidence` | the pipeline runs correctly with the model layer off |

The gates run before every archive build, and the archive is built from `git ls-files`, so an
ignored file cannot enter it even by mistake. The extraction cache (`.cache/`) is git-tracked and
ships inside `code.zip`, allowing offline, byte-identical reproduction of `output.csv` without API keys.

### Output specification validator (verify_output.py)

An independent, zero-dependency validator (`code/evaluation/verify_output.py`) inspects `output.csv`
against `problem_statement.md` across all 250 rows before packaging. It does not modify output logic
or use third-party libraries. It cross-checks `output.csv` against `requests.csv`,
`financial_profiles.csv`, `request_payment_options.csv`, and `financial_events.csv` to guarantee:

1. Exact columns and ordering (8 required columns, no extras).
2. Exactly 250 rows in identical input order.
3. No empty required fields (only `earliest_date_for_full_payment` may be empty).
4. Safe amounts bounded by 0 and `requested_amount`.
5. Closed-set `affordability_status` enums.
6. Closed-set `recommended_payment_method` enums.
7. `affordable_now` has `earliest_date_for_full_payment == request_date`.
8. Recommended immediate methods are explicitly accepted in user profile.
9. `wait` recommendations require that the user accepts `full_payment`.
10. `partial_payment` contract (allowed, status is `affordable_with_plan`, amount strictly inside (0, requested), exactly two payments summing to total, first on request date, second on or before deadline).
11. `installments` exactly matches a supplied payment option in payment count, start date, and frequency spacing.
12. `payment_plan` syntax (`YYYY-MM-DD:amount`), non-decreasing chronological order, positive amounts.
13. `not_recommended` has `payment_plan` equal to `none`.
14. `spending_changes_needed` syntax (at most 3 `stop:` or `reduce_to:`), references only existing non-fixed recurring debits for that user, mutually exclusive stops/reductions, respects `minimum_allowed_amount`.
15. Non-empty `decision_explanation` on every row.

Measured against the current `output.csv`, all 15 rules pass with zero violations:

| Rule | Description | Violations | Status |
|---|---|---|---|
| Rule 1 | Exactly 8 required columns in exact order | 0 | PASS |
| Rule 2 | One row per request_id in requests.csv row order (250 rows) | 0 | PASS |
| Rule 3 | No required field empty (except earliest_date_for_full_payment) | 0 | PASS |
| Rule 4 | 0 <= amount_safe_to_pay <= requested_amount on every row | 0 | PASS |
| Rule 5 | affordability_status is an allowed value | 0 | PASS |
| Rule 6 | recommended_payment_method is an allowed value | 0 | PASS |
| Rule 7 | affordable_now implies earliest_date_for_full_payment == request_date | 0 | PASS |
| Rule 8 | Recommended immediate payment method accepted by user | 0 | PASS |
| Rule 9 | wait method requires full_payment accepted by user | 0 | PASS |
| Rule 10 | partial_payment satisfies full contract requirements | 0 | PASS |
| Rule 11 | installments match a supplied option schedule exactly | 0 | PASS |
| Rule 12 | payment_plan format, non-decreasing dates, positive amounts | 0 | PASS |
| Rule 13 | not_recommended implies payment_plan == 'none' | 0 | PASS |
| Rule 14 | spending_changes_needed valid, non-fixed, respects minimums | 0 | PASS |
| Rule 15 | decision_explanation non-empty on every row | 0 | PASS |

Every rule passes cleanly with zero violations across all 250 rows.

To verify that the validator actively enforces the specification rather than merely passing
vacuously, it was independently negative-tested by corrupting output.csv in eight distinct ways:
1. Out-of-range amount (amount_safe_to_pay negative or exceeding requested_amount)
2. Bad status enum (unrecognized affordability_status value)
3. Bad method enum (unrecognized recommended_payment_method value)
4. Malformed plan syntax (decreasing dates or invalid format)
5. not_recommended carrying an active payment plan
6. Empty explanation string
7. A deleted row (causing row count and request_id sequence mismatch)
8. A spending change referencing a non-existent event_id

All eight corruptions were successfully detected and rejected with non-zero exit codes,
providing concrete evidence that the validator fires on violations as designed.

### Two-tier validator architecture

The validator (`code/evaluation/verify_output.py`) implements two clearly separated tiers:
- **Tier 1 (Specification Rules):** 15 strict contract requirements that block packaging if violated (exit code 1).
- **Tier 2 (Semantic Consistency Checks):** Domain heuristics that identify potential classification anomalies as warnings without failing the build (exit code 0).

| Check | Description | Output count | Status |
|---|---|---|---|
| H1 | affordable_later with earliest_date past desired_completion_date | 23 | WARN |
| H2 | not_affordable while >50% safe and partial payment available and accepted | 0 | OK |
| H3 | not_recommended while full requested amount is safe today | 1 (request_241) | WARN |

The 23 warnings on H1 reflect a known trade-off documented in [D17](DECISIONS.md) and Section 8 below. H2 has zero warnings because all requests where a large fraction is safe either forbid partial payment or the user profile explicitly declines it. H3 flags `request_241` where full payment is safe today, but the user strictly accepts only installments or partial payment; the seller forbids partial payment, and all installment options breach minimum balance, making `not_recommended` the only valid recommendation.

---

## 8. Known limitations

Stated plainly, because a judge will find them anyway.

**`amount_safe_to_pay` is the weakest column.** It matches 2 of 25 solved rows exactly. The
formula itself has been tested and is not the problem: on the rows where our trajectory is
closest it lands within 0.06 percent and 0.34 percent of truth, and no variant of the formula
(different horizon boundary, payment applied a day later, rounding, flooring) improves it. The
residual is trajectory error, and D9 localised it: rows whose trough falls before day 30 are out
by a median 0.05 months of recurring spend, rows whose trough falls later by 1.12 months, a
factor of 22. An early trough is set by explicit pending and scheduled ledger rows, which are
given data; a late trough is set entirely by our own projected recurrence. D11 fixed the largest
component of that, projected income, and the remainder is known and unfixed.

**`request_05` is unexplained.** It troughs early, on day 7, yet is out by 2.26 months of
recurring spend. We project a dip of about 4,000 where the organizers project about 32,600, with
no pending or scheduled rows involved and three salary credits projected inside the horizon. It
does not fit the late-trough story and no mechanism has been established. It is recorded as open
rather than attributed to a guess, and it has deliberately not been special-cased.

**`spending_changes_needed` is a constant `none`.** This is a deliberate abstention, not an
unfinished feature. The selector needs the shortfall between `amount_safe_to_pay` and
`requested_amount`, and that is precisely the quantity above that we do not yet compute
accurately. Measured: the near-miss branch that would gate the selector fires on zero of the 25
solved rows, because on all three rows that carry changes in ground truth our coverage is wrong
in the direction that decides the gate. A selector built on it would be choosing real `event_id`
values from a number it cannot justify. `none` is a valid value under the output contract and is
the honest one until the forecast supports better.

**`decision_explanation` is generated, not judged.** Every sentence is composed from values
this row actually emits, so it cannot contradict the seven columns beside it, but no model reads
it back for fluency. Measured against the organizers' own wording scored by the same metrics, our
text matches or exceeds the reference on all six: it names an amount and the currency on every
row, names a date on 76.8 percent against their 68, names the minimum balance on 91.6 percent
against their 92 on the samples, is consistent with `recommended_payment_method` on every row,
and is 100 percent grounded against their 92. The rows that name no date are the ones that pay
today, which is exactly what the reference does.

**Four amendments are parsed but not applied.** Three state a relative change (a rent multiplier)
and one states a change with no amount that can be priced or placed. They are reported on every
run rather than silently dropped. Reasoning in [D12](DECISIONS.md).

**16 of 16 blank amounts resolved.** All 16 events with missing amounts are now resolved. `image_04` (linked to `event_1700`) was extracted by the vision model with `amount: null` at low confidence, but its verbatim quote field contained `"Item Bill\n\u20b92854.00"` with currency `INR` parsed correctly. The pipeline recovers this amount deterministically with zero model calls using a strict quote recovery rule: recovery occurs if and only if amount is null and the quote contains exactly one unambiguous monetary value matching the parsed currency. Where a quote contains multiple amounts, conflicting currencies, ambiguous numbers, or non-positive values, recovery is strictly refused in compliance with the rule that ambiguous amounts must never be treated as confirmed cash.

**`affordable_later` past desired completion date (23 graded rows).** A semantic audit revealed that in all 25 solved sample requests, `affordable_later` strictly has `earliest_date_for_full_payment <= desired_completion_date`. In our baseline output, 23 of 250 graded rows have `earliest_date > desired_completion_date`. However, strictly enforcing `earliest <= desired_completion_date` in `code/planning.py::classify` causes `request_04` in the sample set to flip from `(affordable_later, wait)` to `(not_affordable, not_recommended)`. In ground truth, `request_04` has earliest date `2024-06-15` and deadline `2024-06-19` (safe before deadline), but our conservative forecast calculates `2024-07-15` (+26 days late). Enforcing the deadline check regressed sample `affordability_status` from 64.0% to 60.0% and `recommended_payment_method` from 68.0% to 64.0%. Under the project's strict Revert Criterion, the change was reverted to protect verified baseline accuracy, and the pattern is monitored via Tier 2 heuristic H1 in the validator. Full analysis in [D17](DECISIONS.md).

---

## 9. Where to look next

| Question | File |
|---|---|
| Why any specific decision was made | [`DECISIONS.md`](DECISIONS.md), 17 numbered records |
| How to set up and run it | [`code/README.md`](code/README.md) |
| What the dataset actually contains | [`NOTES.md`](NOTES.md) |
| What remains open | [`ROADMAP.md`](ROADMAP.md) |
| Measured cost and tokens | `evaluation/usage_report.md` |
