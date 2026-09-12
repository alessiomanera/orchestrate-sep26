"""Tests for income recurrence detection in code/financial_state.py.

These run with no API key and no network: recurrence is detected from the
settled ledger alone, so the arithmetic half of the forecast is testable on
its own.

Each test names a real user in dataset/financial_events.csv, so a regression
here is a regression against the actual data rather than against a fixture
that could drift away from it. The archetypes are the ones measured in
DECISIONS.md D11: a fixed monthly payroll, a payroll interrupted by unpaid
leave, a two-payslip history, and a variable non-monthly earnings stream.

Run with:
    python code/tests/test_income_recurrence.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import financial_state as fs                                  # noqa: E402


def projected_credits(user_id: str, as_of: date) -> list[fs.CashEvent]:
    """Every credit the forecast projects for user_id, evidence layer excluded."""
    state = fs.reconstruct_state(user_id, as_of)
    return [event for event in state.forecast_events if event.amount > 0]


def test_a_clean_monthly_payroll_is_projected():
    """user_24: five identical monthly payslips. The control that already worked."""
    credits = projected_credits("user_24", date(2026, 1, 4))
    assert credits, "a five-month identical payroll must be projected"
    assert all(abs(event.amount - 61000) < 0.01 for event in credits), [e.amount for e in credits]


def test_a_payroll_survives_an_unpaid_leave_gap():
    """user_14: three identical payslips on the 15th, split by a three-month
    leave gap and by a description that changes on return. Neither the gap nor
    the wording ends the employment, so the salary must still be projected."""
    credits = projected_credits("user_14", date(2025, 8, 4))
    assert credits, "a leave gap must not break the series"
    assert all(abs(event.amount - 2717) < 0.01 for event in credits), [e.amount for e in credits]
    assert all(event.event_date.day == 15 for event in credits), [e.event_date for e in credits]


def test_two_monthly_payslips_are_enough_for_income():
    """user_15: a first job, two identical payslips one month apart. Two
    observations a month apart confirm a monthly cadence; the debit threshold
    of three would project no income at all against 49 projected expenses."""
    credits = projected_credits("user_15", date(2026, 1, 6))
    assert credits, "two monthly payslips are evidence of a salary"
    assert all(abs(event.amount - 1661) < 0.01 for event in credits), [e.amount for e in credits]


def test_a_variable_non_monthly_earnings_stream_is_not_projected():
    """user_10: weekly gig payouts under rotating platform names, varying by a
    factor of two. AGENTS.md 6.3 forbids inventing unsupported future income,
    and no stream in this dataset is both non-monthly and stable, so nothing
    here is dependable enough to count on."""
    credits = projected_credits("user_10", date(2024, 12, 6))
    assert credits == [], [(e.event_date, e.amount, e.source) for e in credits]


def test_a_pay_cut_is_projected_at_the_reduced_level():
    """user_67: a payroll that steps down from 2844 to 2047.68. The conservative
    reading of a credit is the lower recent level, never the pre-cut one."""
    credits = projected_credits("user_67", date(2024, 8, 20))
    assert credits, "a payroll that was cut is still a payroll"
    assert all(event.amount <= 2047.68 + 0.01 for event in credits), [e.amount for e in credits]


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
