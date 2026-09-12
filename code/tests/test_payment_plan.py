"""Tests for the payment_plan grammar in code/planning.py.

The grammar is not a free choice, it is read off the 18 solved rows that carry a
plan, so each test names the rows that establish the rule it checks. Runs with no
API key and no network.

Run with:
    python code/tests/test_payment_plan.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import planning                                               # noqa: E402
from planning import Plan, format_payment_plan                # noqa: E402


def test_no_plan_is_the_literal_none():
    """All 7 not_recommended rows in the solved sample render as "none"."""
    assert format_payment_plan(None) == "none"
    assert format_payment_plan(Plan(method="full_payment", payments=[])) == "none"


def test_a_whole_amount_carries_no_decimals():
    """request_23: a requested_amount of 38016 appears as "38016", not "38016.00"."""
    plan = Plan(method="wait", payments=[(date(2025, 7, 15), 38016.0)])
    assert format_payment_plan(plan) == "2025-07-15:38016"


def test_a_fractional_amount_keeps_exactly_two_decimals():
    """request_08 and request_18: a requested_amount of 996.6 appears as "996.60"
    and one of 3246.1 as "3246.10". Trailing zeros are kept here, unlike in the
    amount_safe_to_pay column, which strips them (17229139.2, 603.3, 433.4)."""
    assert format_payment_plan(
        Plan(method="wait", payments=[(date(2025, 4, 15), 996.6)])
    ) == "2025-04-15:996.60"
    assert format_payment_plan(
        Plan(method="wait", payments=[(date(2026, 9, 15), 3246.1)])
    ) == "2026-09-15:3246.10"


def test_entries_are_joined_chronologically_by_a_pipe():
    """request_02: three instalments, oldest first, pipe separated."""
    plan = Plan(
        method="installments",
        payments=[
            (date(2025, 10, 7), 15952906.67),
            (date(2025, 8, 8), 15952906.67),
            (date(2025, 9, 7), 15952906.67),
        ],
    )
    assert format_payment_plan(plan) == (
        "2025-08-08:15952906.67|2025-09-07:15952906.67|2025-10-07:15952906.67"
    )


def test_an_instalment_plan_reproduces_its_supplied_option_exactly():
    """The schedule must come from request_payment_options.csv, never be
    synthesised. request_07's option_19 is 3 payments of 68432 from 2024-09-12
    every 28 days, and the solved row is exactly that."""
    option = {
        "first_payment_date": "2024-09-12",
        "number_of_payments": "3",
        "payment_amount": "68432",
        "payment_frequency_days": "28",
    }
    payments = planning._installment_payments(option, date(2024, 9, 4))
    plan = Plan(method="installments", payments=payments)
    assert format_payment_plan(plan) == "2024-09-12:68432|2024-10-10:68432|2024-11-07:68432"


def test_a_partial_split_is_two_payments_that_sum_to_the_request():
    """request_19: 28820 on the request date, then the remaining 10840 on the
    earliest date a full payment would have been safe. 28820 + 10840 = 39660."""
    requested, safe = 39660.0, 28820.0
    plan = Plan(
        method="partial_payment",
        payments=[(date(2024, 9, 4), safe), (date(2024, 9, 15), requested - safe)],
    )
    assert format_payment_plan(plan) == "2024-09-04:28820|2024-09-15:10840"
    assert abs(sum(amount for _, amount in plan.payments) - requested) < 0.005


def test_a_wait_is_one_full_payment_on_the_first_safe_date():
    """A wait is a plan, not the absence of one. request_03, 04, 08, 13, 18 and
    23 all carry a single full payment on earliest_date_for_full_payment."""
    plan = Plan(method="wait", payments=[(date(2019, 11, 15), 5491000.0)])
    assert format_payment_plan(plan) == "2019-11-15:5491000"


def test_every_solved_row_round_trips_through_the_formatter():
    """The strongest check available: parse each ground-truth plan back into
    payments and reformat it. Any disagreement means the grammar is wrong."""
    import csv

    root = Path(__file__).resolve().parent.parent.parent
    with (root / "dataset" / "sample_requests.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    checked = 0
    for row in rows:
        expected = row["payment_plan"].strip()
        if expected == "none":
            continue
        payments = []
        for entry in expected.split("|"):
            when, _, amount = entry.partition(":")
            payments.append((date.fromisoformat(when), float(amount)))
        assert format_payment_plan(Plan(method="x", payments=payments)) == expected, row["request_id"]
        checked += 1
    assert checked == 18, checked


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")

    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
