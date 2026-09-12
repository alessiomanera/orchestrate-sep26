# Dataset Reconnaissance, Buy or Wait?

Findings from a read-only pass over `dataset/` on 2026-09-12. All numbers verified, not assumed.
Nothing here is solution logic; it is the ground truth the solution has to respect.

## Shape

| File | Rows |
|---|---|
| `requests.csv` | 250 (`request_26` to `request_275`) |
| `sample_requests.csv` | 25 (`request_01` to `request_25`, solved) |
| `financial_profiles.csv` | 275 users |
| `financial_events.csv` | 25,342 |
| `request_payment_options.csv` | 790 (2 to 4 per request, samples included) |
| `messages.csv` | 215 |
| `images.csv` | 16 |
| `exchange_rates.csv` | 134 |

## Integrity, all clean

- Every `requests.user_id` and every `financial_events.user_id` resolves in `financial_profiles.csv`.
- Every request has 2 to 4 payment options. No request is missing options.
- `requests.csv` row order is identical to `dataset/output.csv`. It runs `request_26` to `request_275`;
  it starts at 26 only because 01 to 25 are the solved samples. Still safest to emit rows by iterating
  the template, since that is what the grader diffs against.
- `request_payment_options.csv` also covers `request_01` to `request_25`, so the samples are fully
  reproducible end to end. The evaluation harness can score a real run against them rather than only
  checking the output format.

## Image extraction is exactly 16 calls

16 events have a blank `amount`. There are exactly 16 rows in `images.csv`, each with a
`related_event_id`, and the mapping is 1:1 with no gaps: every blank-amount event has an image, and
every image PNG exists on disk. 15 are `expense`, 1 is `income`.

That bounds the multimodal work completely. One vision pass over 16 PNGs, cached by content hash, and
the blank amounts are solved. Never treat a blank amount as zero.

## Vocabularies

```
event_type   expense 20525 | subscription 2488 | income 1696 | debt_payment 567
             investment_purchase 29 | refund 22 | investment_valuation 10 | investment_sale 5
direction    debit 23609 | credit 1723 | non_cash 10
status       settled 25148 | pending 71 | scheduled 70 | cancelled 22 | failed 21 | unrealized 10
flexibility  fixed 21138 | reducible 2682 | stoppable 1297 | reducible_or_stoppable 225
```

Only the 4,204 non-`fixed` events are candidates for `spending_changes_needed`, and only then if the
category is in the user's willing-to-reduce or willing-to-stop lists and not in their protected list.
`reduce_to:` must respect `minimum_allowed_amount`.

The 194 non-`settled` rows (pending, scheduled, cancelled, failed, unrealized) carry nearly all the
decision risk: reserve pending debits, ignore pending credits, ignore cancelled and failed, and never
count the 10 `unrealized` or `non_cash` investment rows as cash.

## Payment preferences

- `payment_methods_user_will_consider` has 7 distinct combinations. 60 users accept only `full_payment`.
- 119 of 275 users have a blank `max_installment_months`, meaning they will not consider installments at
  all, so 515 of the 790 installment options are unusable for a large share of requests.
- `allows_partial_payment` is `true` for only 80 of 250 requests.

An option existing in `request_payment_options.csv` does not make it eligible.

## Currency

Home currencies: INR 67, EUR 62, IDR 55, ZAR 51, USD 40. 140 events are in a currency other than their
user's home currency, needing 101 distinct `(settlement_date, from, to)` rates, and all 101 are present
in `exchange_rates.csv`. No rate has to be interpolated or inferred.

## Sample answer distribution (the 25 solved rows)

```
affordability_status        affordable_with_plan 9 | not_affordable 7 | affordable_later 6 | affordable_now 3
recommended_payment_method  not_recommended 7 | full_payment 6 | wait 6 | installments 5 | partial_payment 1
spending_changes_needed     3 of 25 are not "none"
```

Answers are spread across every class, so a degenerate constant-output baseline scores poorly, and
`partial_payment` is genuinely rare.

## Two traps

1. The 90-day forecast anchors to each row's `request_date`, not to today. Request dates are 2024 and
   2025. Any use of the system clock is wrong for all 250 rows.
2. `earliest_date_for_full_payment` measures capacity, not preference. Per `problem_statement.md` it can
   equal `request_date` even when the recommendation is `installments`, because the user declined full
   payment. Deriving it from the chosen recommendation loses that column.
