"""Financial state reconstruction and 90-day forecast for Buy or Wait?

Given a user_id and an as-of date (always a request's request_date, never
the system clock, per AGENTS.md section 6.3), reconstructs the user's cash
position and projects it forward across a forecast horizon (default 90 days)
to answer two questions:

- amount_safe_to_pay: the most that can be paid on as_of_date without the
  projected balance ever dropping below minimum_balance_to_keep.
- earliest_date_for_full_payment: the first date a single full payment of a
  given amount would be safe under the same check, independent of whether
  the user would actually choose full payment.

Design decisions, verified against dataset/ before writing this file:

- current_available_balance in financial_profiles.csv is already the user's
  balance at their own request_date. Historical settled events are not
  replayed to rebuild it; they are only used to detect recurrence. Verified:
  no settled/cancelled/failed/unrealized row ever has a settlement_date after
  its user's request_date, across every request.
- Only pending and scheduled rows ever have a settlement_date after
  request_date in this dataset. Those explicit rows carry the near-term
  (median 3 and 9 days out respectively, max 20) commitments. Pending
  credits are excluded (not counted until settled); every other pending or
  scheduled row is applied on its settlement_date.
- The explicit ledger alone does not reach 90 days out (pending settles in a
  median 3 days, scheduled in a median 9, both well under the 90-day
  horizon). Recurring essential debits (rent, subscriptions, debt
  instalments, groceries, ...) are detected from historical settled debits
  grouped by category alone (see _series_key) with at least 3 occurrences
  and a consistent day gap, then projected forward at a conservative amount
  (the larger of the last few occurrences, so a rising or noisy bill is
  never under-forecast). Category, not description, because a variable-cost
  essential like groceries or transport is billed under a different
  description every time yet is still the same recurring commitment;
  checked against user_10, whose weekly groceries and dining never repeat a
  description and were invisible to the forecast until grouping dropped to
  category level. Confirmed salary is detected the same way on the credit
  side (event_type "income"), keeping description in the key and using the
  smaller of the last few occurrences so a rising or noisy paycheck is
  never over-forecast: checked against user_02, whose salary is settled on
  the 15th of every month at an identical amount and description for 5
  straight months with no explicit future row at all, and whose sample
  answer only becomes explicable once that pattern is projected forward.
  Description stays in the key for income specifically because user_10 is
  also a gig worker whose "salary" category rotates between several
  unrelated platform payouts roughly every week; grouping those by category
  alone would manufacture a stable paycheck out of fundamentally
  unpredictable income, which the sample answer for that user confirms is
  wrong (it treats almost none of that income as safe to count on). A
  projection of either kind is dropped if it falls within
  DEDUP_WINDOW_DAYS of an explicit future event of the same series, to
  avoid double-counting a bill or paycheck that already has a known next
  instance. Checked: zero recurring series in this dataset involve a
  foreign currency, so no future exchange rate is ever required for a
  projected date.
- 16 events across the dataset have a blank amount. They are excluded from
  every calculation here (never treated as zero, never guessed) and their
  event_ids are collected on FinancialState.incomplete_event_ids for a later
  step that resolves them from the linked images.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

FORECAST_HORIZON_DAYS = 90

# Recurrence detection tolerances.
MIN_OCCURRENCES_FOR_RECURRENCE = 3
CADENCE_TOLERANCE_DAYS = 5           # absolute slack around the median gap
CADENCE_TOLERANCE_FRACTION = 0.2     # relative slack, whichever is looser
DEDUP_WINDOW_DAYS = 5                # a projection this close to an explicit
                                      # future event of the same series is dropped

DEBIT_RECURRING_TYPES = {"expense", "subscription", "debt_payment"}
CREDIT_RECURRING_TYPES = {"income"}
CASH_STATUSES_FOR_FORECAST = {"pending", "scheduled"}


def _series_key(direction: str, category: str, description: str) -> tuple[str, ...]:
    """Grouping key for recurrence detection.

    Debits group by category alone: a variable-cost essential like groceries
    or transport is billed under a different description every time (a
    different shop, a different fare) but is still the same recurring
    commitment. Credits keep description in the key: income only counts as
    a confirmed, forecastable salary when the same description repeats
    identically. A gig worker's income rotates between several unrelated
    platform payouts under one "salary" category (verified against user_10);
    collapsing those into one series would wrongly manufacture a stable
    paycheck out of fundamentally unpredictable income.
    """
    if direction == "debit":
        return (direction, category)
    return (direction, category, description)


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@dataclass
class CashEvent:
    """One dated, signed, home-currency cash movement in the forecast window."""

    event_date: date
    amount: float  # signed: positive credit, negative debit
    source: str  # "ledger:<event_id>" or "recurring:<category>:<description>"


@dataclass
class FinancialState:
    user_id: str
    as_of_date: date
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    forecast_events: list[CashEvent]
    incomplete_event_ids: list[str]

    @property
    def incomplete(self) -> bool:
        return bool(self.incomplete_event_ids)


# ---------------------------------------------------------------------------
# Dataset loading, cached once per process so a 250-row run reads each file once.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_profiles() -> dict[str, dict]:
    root = repo_root()
    with (root / "dataset" / "financial_profiles.csv").open(newline="", encoding="utf-8") as handle:
        return {row["user_id"]: row for row in csv.DictReader(handle)}


@lru_cache(maxsize=1)
def _load_events_by_user() -> dict[str, list[dict]]:
    root = repo_root()
    by_user: dict[str, list[dict]] = {}
    with (root / "dataset" / "financial_events.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_user.setdefault(row["user_id"], []).append(row)
    return by_user


@lru_cache(maxsize=1)
def _load_rates() -> dict[tuple[str, str, str], float]:
    root = repo_root()
    rates: dict[tuple[str, str, str], float] = {}
    with (root / "dataset" / "exchange_rates.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (row["rate_date"], row["from_currency"], row["to_currency"])
            rates[key] = float(row["rate"])
    return rates


def _convert(amount: float, from_currency: str, to_currency: str, on_date: str) -> float:
    if from_currency == to_currency:
        return amount
    rates = _load_rates()
    direct = rates.get((on_date, from_currency, to_currency))
    if direct is not None:
        return amount * direct
    inverse = rates.get((on_date, to_currency, from_currency))
    if inverse is not None:
        return amount / inverse
    raise KeyError(
        f"no exchange rate for {from_currency}->{to_currency} on {on_date}; "
        "AGENTS.md section 6.1 guarantees every required rate exists, "
        "so a miss here is a lookup bug, not missing data"
    )


# ---------------------------------------------------------------------------
# Recurrence detection
# ---------------------------------------------------------------------------


def _detect_recurring_events(
    history: list[dict],
    home_currency: str,
    as_of_date: date,
    horizon_end: date,
    explicit_dates_by_series: dict[tuple[str, ...], list[date]],
    incomplete_event_ids: list[str],
) -> list[CashEvent]:
    """Project recurring essential cash flows forward from settled history.

    Settled rows on or before as_of_date are grouped by _series_key: debits
    from expense/subscription/debt_payment, credits from income (confirmed
    salary). A series needs at least MIN_OCCURRENCES_FOR_RECURRENCE dated
    occurrences with a consistent gap before it is trusted as recurring.
    """
    series: dict[tuple[str, ...], list[dict]] = {}
    for row in history:
        if row["status"] != "settled":
            continue
        direction = row["direction"]
        if direction == "debit" and row["event_type"] not in DEBIT_RECURRING_TYPES:
            continue
        if direction == "credit" and row["event_type"] not in CREDIT_RECURRING_TYPES:
            continue
        if direction not in ("debit", "credit"):
            continue
        if _parse_date(row["event_date"]) > as_of_date:
            continue
        key = _series_key(direction, row["category"], row["description"])
        series.setdefault(key, []).append(row)

    projected: list[CashEvent] = []
    for key, rows in series.items():
        direction, category = key[0], key[1]
        rows.sort(key=lambda r: r["event_date"])
        if len(rows) < MIN_OCCURRENCES_FOR_RECURRENCE:
            continue

        dated_amounts: list[tuple[date, float]] = []
        for row in rows:
            if row["amount"].strip() == "":
                incomplete_event_ids.append(row["event_id"])
                continue
            amount = _convert(
                float(row["amount"]), row["currency"], home_currency, row["settlement_date"]
            )
            dated_amounts.append((_parse_date(row["event_date"]), amount))
        if len(dated_amounts) < MIN_OCCURRENCES_FOR_RECURRENCE:
            continue

        dates = [d for d, _ in dated_amounts]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        median_gap = sorted(gaps)[len(gaps) // 2]
        if median_gap <= 0:
            continue
        tolerance = max(CADENCE_TOLERANCE_DAYS, median_gap * CADENCE_TOLERANCE_FRACTION)
        if any(abs(gap - median_gap) > tolerance for gap in gaps):
            continue  # not regular enough to trust as recurring

        # Conservative amount: for a debit, the larger of the last few
        # occurrences, so a rising or noisy bill is never under-forecast.
        # For a credit (confirmed salary), the smaller of the last few, so
        # a rising or noisy paycheck is never over-forecast.
        recent = [amount for _, amount in dated_amounts[-3:]]
        conservative_amount = max(recent) if direction == "debit" else min(recent)
        signed_amount = -conservative_amount if direction == "debit" else conservative_amount

        explicit_dates = explicit_dates_by_series.get(key, [])
        next_date = dates[-1] + timedelta(days=median_gap)
        while next_date <= horizon_end:
            if next_date > as_of_date and not any(
                abs((next_date - explicit).days) <= DEDUP_WINDOW_DAYS for explicit in explicit_dates
            ):
                projected.append(
                    CashEvent(next_date, signed_amount, f"recurring:{':'.join(key)}")
                )
            next_date += timedelta(days=median_gap)

    return projected


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconstruct_state(
    user_id: str, as_of_date: date, horizon_days: int = FORECAST_HORIZON_DAYS
) -> FinancialState:
    """Reconstruct user_id's cash position and 90-day forecast as of as_of_date."""
    profile = _load_profiles()[user_id]
    home_currency = profile["home_currency"]
    horizon_end = as_of_date + timedelta(days=horizon_days)
    history = _load_events_by_user().get(user_id, [])

    incomplete_event_ids: list[str] = []
    explicit_events: list[CashEvent] = []
    explicit_dates_by_series: dict[tuple[str, ...], list[date]] = {}

    for row in history:
        status = row["status"]
        if status not in CASH_STATUSES_FOR_FORECAST:
            continue
        settlement = _parse_date(row["settlement_date"])
        if not (as_of_date < settlement <= horizon_end):
            continue
        direction = row["direction"]
        if direction not in ("debit", "credit"):
            continue
        if status == "pending" and direction == "credit":
            continue  # not counted until settled, per AGENTS.md section 6.3

        if row["amount"].strip() == "":
            incomplete_event_ids.append(row["event_id"])
            continue

        amount = _convert(float(row["amount"]), row["currency"], home_currency, row["settlement_date"])
        signed = amount if direction == "credit" else -amount
        explicit_events.append(CashEvent(settlement, signed, f"ledger:{row['event_id']}"))

        is_recurring_type = (direction == "debit" and row["event_type"] in DEBIT_RECURRING_TYPES) or (
            direction == "credit" and row["event_type"] in CREDIT_RECURRING_TYPES
        )
        if is_recurring_type:
            key = _series_key(direction, row["category"], row["description"])
            explicit_dates_by_series.setdefault(key, []).append(settlement)

    recurring_events = _detect_recurring_events(
        history, home_currency, as_of_date, horizon_end, explicit_dates_by_series, incomplete_event_ids
    )

    return FinancialState(
        user_id=user_id,
        as_of_date=as_of_date,
        home_currency=home_currency,
        current_available_balance=float(profile["current_available_balance"]),
        minimum_balance_to_keep=float(profile["minimum_balance_to_keep"]),
        forecast_events=sorted(explicit_events + recurring_events, key=lambda e: e.event_date),
        incomplete_event_ids=sorted(set(incomplete_event_ids)),
    )


def _group_by_date(events: list[CashEvent]):
    events = sorted(events, key=lambda e: e.event_date)
    i = 0
    while i < len(events):
        current = events[i].event_date
        group = []
        while i < len(events) and events[i].event_date == current:
            group.append(events[i])
            i += 1
        yield current, group


def _balance_timeline(state: FinancialState) -> list[tuple[date, float]]:
    """[(date, balance after all events up to and including that date), ...].

    Starts with (as_of_date, current_available_balance), then one entry per
    distinct future forecast date, in chronological order.
    """
    timeline = [(state.as_of_date, state.current_available_balance)]
    running = state.current_available_balance
    for event_date, group in _group_by_date(state.forecast_events):
        running += sum(event.amount for event in group)
        timeline.append((event_date, running))
    return timeline


def amount_safe_to_pay(state: FinancialState, requested_amount: float) -> float:
    """Largest amount payable on as_of_date that never breaches the minimum
    balance at any point in the forecast horizon, capped at requested_amount.

    A payment today is a constant one-time reduction applied at every future
    point, so the binding constraint is the single worst (most negative)
    point the cumulative future cash flow ever reaches.
    """
    timeline = _balance_timeline(state)
    worst_dip = min(balance - state.current_available_balance for _, balance in timeline)
    safe_today = state.current_available_balance - state.minimum_balance_to_keep + worst_dip
    return max(0.0, min(requested_amount, safe_today))


def earliest_date_for_full_payment(state: FinancialState, requested_amount: float) -> date | None:
    """First date a single payment of requested_amount is safe for the rest
    of the forecast horizon, or None if it never is within the horizon.

    This measures capacity only: it does not consider whether the user's
    payment preferences would actually permit full_payment.
    """
    timeline = _balance_timeline(state)
    suffix_min = [0.0] * len(timeline)
    suffix_min[-1] = timeline[-1][1]
    for i in range(len(timeline) - 2, -1, -1):
        suffix_min[i] = min(timeline[i][1], suffix_min[i + 1])

    for (candidate_date, _), floor in zip(timeline, suffix_min):
        if floor - requested_amount >= state.minimum_balance_to_keep:
            return candidate_date
    return None
