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
        relative_change=None,
        scope_category=None,
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


def test_only_an_employer_can_end_a_salary():
    """message_17 measured: a prize administrator telling user_24 the claim is
    closed and "there are no further scheduled payments" was ending that user's
    employment income, turning an accurate row into the worst in the sample. A
    counterparty can only cancel an obligation it is party to."""
    salary = [fs.CashEvent(date(2025, 2, 15), 61000, "recurring", series="credit")]
    outsider = fact(kind="cancel", target="recurring_income", source_type="financial_service")
    assert dated(apply([outsider], list(salary))) == dated(salary)


def test_an_employer_can_still_end_a_salary():
    """The guard must not disarm the legitimate case: 13 of the 14 cancellations
    aimed at recurring income in this dataset come from an employer."""
    salary = [fs.CashEvent(date(2025, 2, 15), 61000, "recurring", series="credit")]
    employer = fact(kind="cancel", target="recurring_income", source_type="employer")
    assert apply([employer], list(salary)) == []


def test_an_outsider_still_acts_on_the_row_it_names():
    """message_17 names event_2165, the prize credit it is actually about. The
    guard redirects the message to that row rather than discarding it."""
    events = [
        fs.CashEvent(date(2025, 2, 15), 61000, "recurring", series="credit"),
        fs.CashEvent(date(2025, 2, 20), 33550, "ledger:event_2165", event_id="event_2165"),
    ]
    outsider = fact(
        kind="cancel", target="recurring_income",
        source_type="financial_service", related_event_id="event_2165",
    )
    result = apply([outsider], list(events))
    assert [e.amount for e in result] == [61000], [(e.event_id, e.amount) for e in result]
    assert all(e.event_id != "event_2165" for e in result)


def test_a_percentage_amendment_scales_the_recurring_amount():
    """message_12, 105, 147 and 175: "the renewed lease increases monthly rent
    by 12%". The extraction schema has no field for a relative change, so these
    carried no amount and were silently dropped."""
    events = [
        fs.CashEvent(date(2025, 2, 1), -1000, "recurring:debit:rent", series="debit"),
        fs.CashEvent(date(2025, 3, 1), -1000, "recurring:debit:rent", series="debit"),
        fs.CashEvent(date(2025, 2, 5), -200, "recurring:debit:groceries", series="debit"),
    ]
    amendment = fact(
        kind="amend", target="recurring_expense", source_type="service_provider",
        relative_change=1.12, scope_category="rent",
    )
    result = apply([amendment], list(events))
    assert sorted(round(e.amount, 2) for e in result) == [-1120.0, -1120.0, -200.0],         [(e.source, e.amount) for e in result]


def test_a_percentage_that_cannot_be_placed_is_not_broadcast():
    """message_175 says "sewa", not a category name this pipeline knows. A rent
    rise applied to groceries, transport and every other category is a worse
    error than not applying it: measured, that broadcast alone flipped
    request_16 from affordable_now to not_affordable."""
    events = [
        fs.CashEvent(date(2025, 2, 1), -1000, "recurring:debit:rent", series="debit"),
        fs.CashEvent(date(2025, 2, 5), -200, "recurring:debit:groceries", series="debit"),
    ]
    amendment = fact(
        kind="amend", target="recurring_expense", source_type="service_provider",
        relative_change=1.12, scope_category=None,
    )
    assert sorted(e.amount for e in apply([amendment], list(events))) == [-1000, -200]


def test_an_absolute_amount_wins_over_a_percentage():
    """If the message states the new amount outright, that is the fact; the
    percentage is only a fallback for when it does not."""
    rent = [fs.CashEvent(date(2025, 2, 1), -1000, "recurring:debit:rent", series="debit")]
    amendment = fact(
        kind="amend", target="recurring_expense", new_recurring_amount=1500,
        source_type="service_provider", relative_change=1.12, scope_category="rent",
    )
    assert [e.amount for e in apply([amendment], list(rent))] == [-1500]


def test_relative_change_is_parsed_in_any_language():
    """message_175 is Indonesian. The parser reads the number and the sign word
    it is attached to, so it must not depend on English prose."""
    from evidence import _relative_change
    assert _relative_change("The renewed lease increases monthly rent by 12%.") == 1.12
    assert _relative_change("Perpanjangan sewa menaikkan biaya sewa bulanan sebesar 12%.") == 1.12
    assert _relative_change("Your rent is reduced by 5 percent.") == 0.95
    assert _relative_change("Your next salary is reduced to EUR 1222.65.") is None


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
