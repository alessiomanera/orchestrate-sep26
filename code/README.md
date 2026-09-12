# Buy or Wait? - solution

An agent that decides, for each request in `dataset/requests.csv`, whether the user should pay
in full, pay partially, use an installment option, wait, or not proceed.

## Setup

Python 3.12. No third-party packages are required to run the solution: it uses only the
standard library. (`pandas` appears in the development environment but the pipeline does not
import it.)

```bash
python -m venv .venv
.venv/Scripts/python.exe -V     # Windows
```

The evidence layer calls a model and needs one credential. Copy the template and fill it in:

```bash
cp .env.example .env
```

| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY` | required for the evidence layer; read from the environment only |
| `VISION_MODEL`, `TEXT_MODEL` | model ids, default `gemini-3.8-flash` |
| `MODEL_FALLBACKS` | comma-separated models tried when the primary is out of quota |
| `MODEL_TEMPERATURE` | `0`, for determinism |
| `CACHE_DIR` | where extraction results are cached, default `.cache` |
| `INPUT_USD_PER_MTOK`, `OUTPUT_USD_PER_MTOK` | prices used by the usage report |

No key is ever read from a file other than `.env`, and none is logged, printed or committed.

## Run

```bash
python code/main.py                  # dataset/requests.csv  -> output.csv
python code/main.py --sample         # the 25 solved rows    -> code/evaluation/sample_output.csv
python code/main.py --no-evidence    # ablation: ledger only, no model calls, no key needed
python code/evaluation/main.py       # self-score against the 25 solved rows
python code/tests/test_evidence_applier.py
python code/package_submission.py --check    # submission gates
```

`--no-evidence` runs the whole pipeline with the model layer switched off. It needs no key and
no network, which makes it both the offline fallback and the control arm when measuring what
the evidence layer actually contributes.

## How it works

A deterministic financial core with a narrow model layer around the two genuinely unstructured
inputs. The reasoning behind that split, and every other significant choice, is in
`DECISIONS.md` at the repository root.

| File | Responsibility |
|---|---|
| `code/main.py` | reads the requests, writes the eight output columns |
| `code/financial_state.py` | balance reconstruction, 90-day forecast, evidence application |
| `code/evidence.py` | prompts and closed-vocabulary schemas for images and messages |
| `code/llm.py` | model access: REST, batching, caching, retry, token accounting |
| `code/usage.py` | writes `code/evaluation/usage_report.md` from measured token counts |
| `code/evaluation/main.py` | self-scoring harness against the solved sample rows |
| `code/tests/` | tests for the evidence applier, no key or network needed |

**The forecast.** Each request is answered as of its own `request_date`, never the system clock.
The profile's `current_available_balance` is the starting point; pending and scheduled rows are
applied on their settlement dates (pending credits are not counted until they settle); and
recurring commitments are projected forward from settled history where the cadence is
consistent enough to trust. The largest amount payable today is the one that never breaches
`minimum_balance_to_keep` at any point in the horizon.

**The evidence layer.** 16 events have a blank amount and each has a linked document image; a
vision pass recovers the totals, and a blank is never treated as zero. The 215 messages are
classified into typed records (amend, cancel, delay, confirm, clarify, irrelevant) and folded
into the forecast through the conflict order in `problem_statement.md`.

Both surfaces are dataset-sized, not request-sized: 231 documents, read once and cached by
content hash, whatever the request count. Re-running the pipeline spends nothing.

**Untrusted input.** Message and image content is data, never instruction. It travels inside a
delimited block, the model answers in a server-enforced schema of enums and numbers, the one
free-text field is kept for auditing and never reaches a decision or an output column, and every
extracted fact is arbitrated by deterministic Python before it can change a number. See
`DECISIONS.md` D3.

**Failure behaviour.** Every model call is bounded: capped attempts, exponential backoff, a
model fallback chain when one is out of quota or under load, and a circuit breaker that stops
the run from grinding against an exhausted quota. Every failure path degrades that document to
"no evidence", which is exactly the `--no-evidence` behaviour, so a failed extraction can never
be recorded as a successful one.

## Status

As of 2026-09-12, `amount_safe_to_pay` and `earliest_date_for_full_payment` are produced by the
forecast and the evidence layer. `affordability_status`, `recommended_payment_method`,
`payment_plan` and `spending_changes_needed` are still safe placeholders: the eligibility filter
and plan ranking are the next step. `ROADMAP.md` tracks what remains.
