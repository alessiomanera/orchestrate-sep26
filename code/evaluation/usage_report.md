# Usage report

Generated 2026-09-12 19:09 UTC from the provider's own `usageMetadata`, recorded per call at the moment of the call.

## Provider and models

| Surface | Provider | Model | Purpose |
|---|---|---|---|
| images | Google | `gemini-3.6-flash` | read the total from each linked document image |
| images | Google | `gemini-3.7-flash` | read the total from each linked document image |
| messages | Google | `gemini-3.6-flash` | classify each message into a typed financial fact |
| messages | Google | `gemini-3.7-flash` | classify each message into a typed financial fact |

Every model named above is one that actually answered, taken from the provider's `modelVersion` in the response, not the model configured in `.env`. The two can differ: the provider caps free-tier requests at 20 per day per model, so a run that exhausts its configured head continues down a fallback chain. More than one model per surface is therefore expected, and a configured model that answered nothing does not appear here at all.

## Documents, calls and tokens

Documents are the things read: 16 images and 215 messages. Calls are the requests actually made, which is far smaller because documents are batched (25 messages or 4 images per call).

| Surface | Model | Documents | Calls | Input tokens | Output tokens | Total |
|---|---|---:|---:|---:|---:|---:|
| images | `gemini-3.6-flash` | 12 | 3 | 14,848 | 912 | 15,760 |
| images | `gemini-3.7-flash` | 4 | 1 | 4,980 | 356 | 5,336 |
| messages | `gemini-3.6-flash` | 140 | 6 | 18,750 | 17,380 | 36,130 |
| messages | `gemini-3.7-flash` | 75 | 3 | 9,775 | 10,850 | 20,625 |
| **total** | | **231** | **13** | **48,353** | **29,498** | **77,851** |

Batched calls report one token count for the whole batch, so each document's row above carries an even share of its batch's tokens. The totals are exact; the per-document split is an even attribution.

Failed attempts (a model out of quota, or briefly unavailable) consume a request against the provider's cap but return no usage metadata, so they cannot appear in these token counts. The figures describe the calls that produced the results.

## Per request

- Requests in the graded run: **250**
- Total tokens: **77,851**
- Average tokens per request: **311.4**
- Model calls in the whole run: **13**

Model calls do not scale with the number of requests. Both surfaces are dataset-sized, not request-sized: 231 documents read once and cached, whatever the request count. The per-request figures above are therefore the total divided across the run, not a per-row cost, and they fall as the request count rises.

## Estimated cost

Rates applied: $0.30 per 1M input tokens, $2.50 per 1M output tokens. These two rates are the only assumed values in this report; every token count above is measured.

- Estimated total cost: **$0.0883**
- Estimated cost per request: **$0.000353**

## Cost of a re-run

Documents read live in this run: **0** of 231 (231 served from the content-hash cache). Marginal cost of this run: **$0.0000**.

A fully warm cache makes the pipeline free to re-run and keeps these figures reproducible, which is why cache hits are recorded here rather than dropped.

