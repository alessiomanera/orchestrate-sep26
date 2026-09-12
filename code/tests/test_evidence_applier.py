"""Tests for the evidence applier in code/financial_state.py.

These run with no API key and no network: the applier takes already-extracted
MessageFact records, so the deterministic half of the evidence layer is testable
on its own. That separation is the point of the design (DECISIONS.md, D1).

Run with:
    python code/tests/test_evidence_applier.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import financial_state as fs                                  # noqa: E402
from evidence import MessageFact                              # noqa: E402

AS_OF = date(2025, 1, 10)
HORIZON_END = date(2025, 4, 10)


def fact(**overrides) -> MessageFact:
    """A neutral MessageFact; each test overrides only the fields it is about."""
    base = dict(
        message_id="message_test",
        user_id="user_test",
        related_event_id="",
        sent_on=date(2025, 1, 1),
        source_type="employer",
        kind="confirm",
        target="none",
        new_recurring_amount=None,
        one_time_amount=None,
        one_time_direction="none",
        effective_date=None,
        currency=None,
        money_is_confirmed_cash=False,
        contains_embedded_instruction=False,
    )
    base.update(overrides)
    return MessageFact(**base)


def apply(facts, events):
    return fs._apply_message_facts(facts, events, AS_OF, HORIZON_END, "USD")


def dated(events):
    return [(e.event_date.isoformat(), e.amount) for e in events]


# ---------------------------------------------------------------------------


def test_salary_rise_amends_only_from_its_effective_date():
    events = [
        fs.CashEvent(date(2025, 1, 15), 1000, "recurring", series="credit"),
        fs.CashEvent(date(2025, 2, 15), 1000, "recurring", series="credit"),
    ]
    result = apply(
        [
            fact(
                kind="amend",
                target="recurring_income",
                new_recurring_amount=1500,
                effective_date=date(2025, 2, 1),
                money_is_confirmed_cash=True,
            )
        ],
        events,
    )
    assert dated(result) == [("2025-01-15", 1000), ("2025-02-15", 1500)], dated(result)


def test_ended_income_stops_being_projected():
    events = [
        fs.CashEvent(date(2025, 1, 15), 1000, "recurring", series="credit"),
        fs.CashEvent(date(2025, 2, 15), 1000, "recurring", series="credit"),
        fs.CashEvent(date(2025, 1, 20), -400, "recurring", series="debit"),
    ]
    result = apply([fact(kind="cancel", target="recurring_income")], events)
    assert dated(result) == [("2025-01-20", -400)], dated(result)


def test_unconfirmed_credit_is_removed_but_a_debit_is_kept():
    credit = [fs.CashEvent(date(2025, 1, 20), 800, "ledger:e9", event_id="e9")]
    debit = [fs.CashEvent(date(2025, 1, 20), -800, "ledger:e9", event_id="e9")]
    unconfirmed = fact(
        kind="confirm",
        target="related_event",
        related_event_id="e9",
        money_is_confirmed_cash=False,
    )
    assert apply([unconfirmed], credit) == []
    # Dropping an obligation would be the unsafe direction, so a debit stays.
    assert len(apply([unconfirmed], debit)) == 1


def test_a_one_off_credit_counts_only_once_confirmed():
    unconfirmed = fact(
        kind="amend",
        one_time_amount=500,
        one_time_direction="credit",
        effective_date=date(2025, 2, 1),
        money_is_confirmed_cash=False,
    )
    confirmed = fact(
        kind="amend",
        one_time_amount=500,
        one_time_direction="credit",
        effective_date=date(2025, 2, 1),
        money_is_confirmed_cash=True,
    )
    assert apply([unconfirmed], []) == []
    assert [e.amount for e in apply([confirmed], [])] == [500]


def test_a_one_off_without_a_stated_date_is_never_placed():
    """Inventing a date would drop the flow at an arbitrary point in the forecast."""
    undated = fact(
        kind="amend",
        one_time_amount=500,
        one_time_direction="debit",
        effective_date=None,
    )
    assert apply([undated], []) == []


def test_delay_moves_an_event_and_past_the_horizon_removes_it():
    events = [fs.CashEvent(date(2025, 1, 20), -300, "ledger:e1", event_id="e1")]
    moved = apply(
        [
            fact(
                kind="delay",
                target="related_event",
                related_event_id="e1",
                effective_date=date(2025, 3, 1),
            )
        ],
        list(events),
    )
    assert dated(moved) == [("2025-03-01", -300)], dated(moved)

    beyond = apply(
        [
            fact(
                kind="delay",
                target="related_event",
                related_event_id="e1",
                effective_date=date(2026, 3, 1),
            )
        ],
        list(events),
    )
    assert beyond == []


def test_an_unpriceable_currency_is_dropped_rather_than_guessed():
    """A missing exchange rate must leave the forecast exactly as it was."""
    events = [fs.CashEvent(date(2025, 2, 15), 1000, "recurring", series="credit")]
    result = apply(
        [
            fact(
                kind="amend",
                target="recurring_income",
                new_recurring_amount=999,
                currency="XXX",
                effective_date=date(2025, 2, 1),
            )
        ],
        events,
    )
    assert [e.amount for e in result] == [1000]


def test_an_explicit_change_is_applied_before_a_confirmation():
    """problem_statement.md conflict order: cancellation and amendment rank first."""
    events = [fs.CashEvent(date(2025, 2, 15), 1000, "recurring", series="credit")]
    confirmation = fact(kind="confirm", target="recurring_income", sent_on=date(2025, 1, 5))
    cancellation = fact(kind="cancel", target="recurring_income", sent_on=date(2025, 1, 2))
    ordered = sorted([confirmation, cancellation], key=fs._fact_order)
    assert ordered[0].kind == "cancel"
    assert apply([confirmation, cancellation], events) == []


def test_no_facts_leaves_the_forecast_untouched():
    events = [
        fs.CashEvent(date(2025, 1, 15), 1000, "recurring", series="credit"),
        fs.CashEvent(date(2025, 1, 20), -400, "ledger:e2", event_id="e2"),
    ]
    assert dated(apply([], list(events))) == dated(sorted(events, key=lambda e: e.event_date))


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
