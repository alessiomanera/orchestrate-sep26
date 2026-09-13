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
| `code/package_submission.py` | 216 | submission gates and archive build | anything the pipeline runs |

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
falsified hypothesis that was kept rather than quietly dropped. The layer resolves 15 of 16 blank
event amounts and classifies all 215 messages into typed facts. The pipeline still runs correctly
without it: `python code/main.py --no-evidence` is both the offline fallback and the control arm.

---

## 4. Determinism, and where it stops

**What is guaranteed.**

| Property | Mechanism |
|---|---|
| Same input, same output | forecast, eligibility and ranking are pure Python, no model in the decision path |
| Zero live calls on re-run | every document cached under a SHA-256 of exactly what was sent |
| Byte-identical `output.csv` | deterministic rendering from a warm cache |
| Sampling pinned | temperature 0 on every call |
| No wall-clock dependence | every request is answered as of its own `request_date`, never `today` |
| No third-party drift | the whole solution imports only the Python standard library |

**What is not guaranteed, stated plainly.** *Which model* reads a given document is not
deterministic. The provider's free tier caps `generateContent` at 20 requests per day per model,
so the run walks a fallback chain when its head is exhausted. Where a document lands in that
chain depends on remaining quota, which is wall-clock state outside our control.

This is not hypothetical. Measured from the cache, the configured head `gemini-3.8-flash`
answered **zero** of the 231 documents; every one was served by a fallback. `usage_report.md`
therefore reports the `modelVersion` the provider returns with each response, not the model
configured in `.env`, because the latter would have named a model that did no work. Full record
in [D8](DECISIONS.md).

The distinction worth holding: the *pipeline* is reproducible, the *model selection* is not. Once
the cache is warm the second point stops mattering, which is why cache hits are reported rather
than dropped.

---

## 5. Failure and abstention behaviour

The rule throughout: **a failure must never be recorded as a success, and an unknown must never
be rendered as a zero.**

| Situation | Behaviour |
|---|---|
| Image unreadable | the amount stays blank and is flagged `[unread]`, never zeroed. `image_04` is currently in this state and says so on every run. |
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
| `python code/package_submission.py --check` | eight gates: column order, one row per request, row order matches the dataset, no private file tracked, transcript leak scan, usage report is real |
| `python code/evaluation/main.py` | self-scores the seven comparable columns against the 25 solved rows, and scores `decision_explanation` on usefulness and consistency against the row we emitted |
| `code/tests/*.py` | 29 checks, no key and no network, plain scripts with no test runner |
| `python code/main.py --no-evidence` | the pipeline runs correctly with the model layer off |

The gates run before every archive build, and the archive is built from `git ls-files`, so an
ignored file cannot enter it even by mistake.

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

**One image is unread.** `image_04` did not yield a confident amount. The linked event's amount
stays blank rather than being treated as zero.

---

## 9. Where to look next

| Question | File |
|---|---|
| Why any specific decision was made | [`DECISIONS.md`](DECISIONS.md), 15 numbered records |
| How to set up and run it | [`code/README.md`](code/README.md) |
| What the dataset actually contains | [`NOTES.md`](NOTES.md) |
| What remains open | [`ROADMAP.md`](ROADMAP.md) |
| Measured cost and tokens | `evaluation/usage_report.md` |
