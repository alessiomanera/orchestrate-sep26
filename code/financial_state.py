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
  category level.
- Income (event_type "income") is projected on different rules from expense,
  because a salary is a different kind of obligation from a grocery bill
  (DECISIONS.md D11). It is projected only when it settles at most once per
  calendar month, always on the same day of the month, which is what a
  monthly commitment looks like and what a gig payout stream does not; and
  two such settlements are enough, against three for a debit, because two
  payslips a month apart already establish a monthly cadence whereas two
  similar shop receipts a month apart may be coincidence. It is projected at
  the smaller of the last few occurrences, so a rising or noisy paycheck is
  never over-forecast and a pay cut is tracked down to its new level. Skipped
  months are tolerated: unpaid leave interrupts a salary, it does not end it.
  Every one of these boundaries was measured across all 275 users before it
  was adopted, and the alternatives that were tested and rejected are recorded
  in D11 and in _series_key. A projection of either kind is dropped if it falls within
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

from evidence import EMPTY_EVIDENCE, Evidence, MessageFact

FORECAST_HORIZON_DAYS = 90

# Recurrence detection tolerances.
MIN_OCCURRENCES_FOR_RECURRENCE = 3   # debits: three occurrences before a series is trusted
MIN_OCCURRENCES_FOR_INCOME = 2       # credits: two, see _detect_recurring_events
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
    commitment; checked against user_10, whose weekly groceries and dining
    never repeat a description.

    Credits keep description, and two alternatives were measured and rejected
    (DECISIONS.md D11). Dropping it entirely, so credits group by category
    like debits, looks tidier and is wrong: several users draw a fixed monthly
    base salary and an irregular bonus or commission under the same "salary"
    category (user_03, user_04, user_11, user_13). Merging them puts two
    settlements in one calendar month, which fails _monthly_schedule and
    discards the base salary too, moving those four rows out by 1.6 to 2.7
    months of recurring spend. Replacing it with the day of the month is worse
    still: a weekly payer lands on roughly four fixed days each month, and each
    of those days then looks like its own monthly salary.

    What actually separates a salary from a gig payout is cadence, not wording,
    and that check lives in _monthly_schedule.
    """
    if direction == "debit":
        return (direction, category)
    return (direction, category, description)


def _month_index(day: date) -> int:
    """Calendar months since year zero, so month arithmetic never wraps badly."""
    return day.year * 12 + (day.month - 1)


def _add_months(day: date, months: int, day_of_month: int) -> date:
    """day shifted by whole calendar months, landing on day_of_month.

    Clamped to the length of the target month, so a salary paid on the 31st
    lands on the 28th or 30th rather than overflowing into the next month.
    """
    index = _month_index(day) + months
    year, month = divmod(index, 12)
    month += 1
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month + 1, 1)
    days_in_month = (next_month_start - date(year, month, 1)).days
    return date(year, month, min(day_of_month, days_in_month))


def _monthly_schedule(dates: list[date]) -> int | None:
    """The day of the month a series settles on, or None if it is not monthly.

    A monthly commitment settles at most once in any calendar month, always on
    the same day of the month. Measured across all 275 users: of the 268 income
    series with two or more settled events, 196 satisfy this and all 196 land on
    an identical day of month (spread of zero days, no exceptions). The other 72
    are weekly, nine-day or twice-monthly earnings streams whose amounts vary by
    a median of 61 percent.

    Skipped months are allowed and are the point of the test. A payroll
    interrupted by unpaid leave (user_14: March, April, then July) is still the
    same monthly salary; a rule built on the gap between consecutive events
    reads that three-month gap as a quarterly cadence and discards the series.
    Judging the calendar rather than the gap makes an interruption a missed
    payment instead of a different job.
    """
    if len(dates) < 2:
        return None
    months = [_month_index(day) for day in dates]
    if len(set(months)) != len(months):
        return None  # two settlements in one calendar month is not monthly
    days_of_month = {day.day for day in dates}
    if len(days_of_month) != 1:
        return None
    return days_of_month.pop()


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
    source: str  # "ledger:<event_id>", "recurring:<key>" or "evidence:<message_id>"
    event_id: str | None = None       # set for rows that came from the ledger
    series: str | None = None         # "debit" or "credit" for projected recurring rows


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


def _row_amount(row: dict, evidence: "Evidence") -> tuple[float, str] | None:
    """(amount, currency) for a ledger row, or None if it is still unknown.

    A blank amount is resolved from the linked document image when the evidence
    layer read one. If it did not, the row stays unknown and every caller skips
    it: a blank is never silently treated as zero (AGENTS.md section 6.1).
    """
    raw = row["amount"].strip()
    if raw:
        return float(raw), row["currency"]
    resolved = evidence.amount_by_event_id.get(row["event_id"])
    return resolved if resolved else None


def _detect_recurring_events(
    history: list[dict],
    home_currency: str,
    as_of_date: date,
    horizon_end: date,
    explicit_dates_by_series: dict[tuple[str, ...], list[date]],
    incomplete_event_ids: list[str],
    evidence: "Evidence",
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
        direction = key[0]
        rows.sort(key=lambda r: r["event_date"])
        minimum_occurrences = (
            MIN_OCCURRENCES_FOR_RECURRENCE if direction == "debit" else MIN_OCCURRENCES_FOR_INCOME
        )
        if len(rows) < minimum_occurrences:
            continue

        dated_amounts: list[tuple[date, float]] = []
        for row in rows:
            resolved = _row_amount(row, evidence)
            if resolved is None:
                incomplete_event_ids.append(row["event_id"])
                continue
            raw_amount, currency = resolved
            amount = _convert(raw_amount, currency, home_currency, row["settlement_date"])
            dated_amounts.append((_parse_date(row["event_date"]), amount))
        if len(dated_amounts) < minimum_occurrences:
            continue

        dates = [d for d, _ in dated_amounts]

        # Conservative amount: for a debit, the larger of the last few
        # occurrences, so a rising or noisy bill is never under-forecast.
        # For a credit (confirmed salary), the smaller of the last few, so
        # a rising or noisy paycheck is never over-forecast, and a pay cut is
        # tracked down to its new level rather than averaged away.
        recent = [amount for _, amount in dated_amounts[-3:]]
        conservative_amount = max(recent) if direction == "debit" else min(recent)
        signed_amount = -conservative_amount if direction == "debit" else conservative_amount

        explicit_dates = explicit_dates_by_series.get(key, [])
        source = f"recurring:{':'.join(key)}"

        def emit(when: date) -> None:
            if when > as_of_date and not any(
                abs((when - explicit).days) <= DEDUP_WINDOW_DAYS for explicit in explicit_dates
            ):
                projected.append(CashEvent(when, signed_amount, source, series=direction))

        if direction == "credit":
            # Income is projected only from a monthly salary. Measured across
            # all 275 users, every income series in this dataset that settles
            # at most once per calendar month does so on an identical day of
            # the month, and every series that does not is an earnings stream
            # varying by a median of 61 percent: weekly delivery payouts,
            # twice-monthly freelance milestones. Those are real income but
            # they are not a dependable future commitment, and AGENTS.md 6.3
            # forbids inventing unsupported future income, so they are left out
            # of the forecast rather than averaged into it.
            day_of_month = _monthly_schedule(dates)
            if day_of_month is None:
                continue
            step = 1
            while True:
                next_date = _add_months(dates[-1], step, day_of_month)
                if next_date > horizon_end:
                    break
                emit(next_date)
                step += 1
            continue

        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        median_gap = sorted(gaps)[len(gaps) // 2]
        if median_gap <= 0:
            continue
        tolerance = max(CADENCE_TOLERANCE_DAYS, median_gap * CADENCE_TOLERANCE_FRACTION)
        if any(abs(gap - median_gap) > tolerance for gap in gaps):
            continue  # not regular enough to trust as recurring

        next_date = dates[-1] + timedelta(days=median_gap)
        while next_date <= horizon_end:
            emit(next_date)
            next_date += timedelta(days=median_gap)

    return projected


# ---------------------------------------------------------------------------
# Evidence application
#
# The extracted facts do not overwrite the ledger. They are applied here, in
# the conflict order problem_statement.md specifies: an explicit cancellation,
# settlement or amendment first, then the newer record from the same source,
# then a settled event over an estimate, then the financially safer reading.
# ---------------------------------------------------------------------------

# An explicit change outranks a confirmation, which outranks nothing at all.
FACT_PRIORITY = {"cancel": 0, "amend": 0, "delay": 0, "confirm": 1, "clarify": 1, "irrelevant": 2}

# Who is allowed to end or rewrite a salary. A counterparty can only change an
# obligation it is party to: an employer can stop paying you, a prize
# administrator cannot. Measured across all 215 messages, 99 of the 101
# cancellations, amendments and delays aimed at recurring income come from an
# employer, so this guard leaves the ordinary case untouched and bites only on
# the two that are misattributed (message_17 and message_86, both from a
# financial_service). message_17 alone was the largest single error in the
# solved sample: it read a closed prize claim as the end of a 61,000 a month
# salary and drove that user's projected trough from +64,420 to -84,504.
INCOME_AUTHORITY = {"employer"}
CHANGING_KINDS = ("cancel", "amend", "delay")


def _fact_order(fact: "MessageFact") -> tuple[int, date]:
    """Sort key. Lower tier applies first; inside a tier, older applies first so
    the newest record from the same source is the one left standing."""
    return (FACT_PRIORITY.get(fact.kind, 2), fact.sent_on)


def _fact_amount_in_home(
    amount: float | None, fact: "MessageFact", home_currency: str, on_date: date
) -> float | None:
    """Convert a message's stated amount into the home currency.

    Returns None if the message quotes a foreign currency for which the fixed
    rate table has no row on that date. A missing rate is never interpolated or
    assumed: the fact is simply dropped, which leaves the forecast exactly as it
    was without the message.
    """
    if amount is None:
        return None
    currency = (fact.currency or home_currency).strip().upper() or home_currency
    if currency == home_currency:
        return amount
    try:
        return _convert(amount, currency, home_currency, on_date.isoformat())
    except KeyError:
        return None


def _apply_message_facts(
    facts: list["MessageFact"],
    events: list[CashEvent],
    as_of_date: date,
    horizon_end: date,
    home_currency: str,
) -> list[CashEvent]:
    """Fold message evidence into the forecast. Returns the revised event list.

    Every operation is conservative in the same direction the challenge rules
    are: money is only added to the forecast when the message says it is
    confirmed, and money is removed whenever a message says a flow has stopped.
    """
    events = list(events)

    for fact in sorted(facts, key=_fact_order):
        effective = fact.effective_date or as_of_date

        target = fact.target
        if (
            target == "recurring_income"
            and fact.kind in CHANGING_KINDS
            and fact.source_type not in INCOME_AUTHORITY
        ):
            # Not this sender's obligation to end. The message is not discarded:
            # if it names a ledger row it is demoted to acting on that row only,
            # which is what it was really about.
            target = "related_event" if fact.related_event_id else "none"

        # --- a recurring flow changes or stops -----------------------------
        if target in ("recurring_income", "recurring_expense"):
            wanted = "credit" if target == "recurring_income" else "debit"
            matching = [e for e in events if e.series == wanted and e.event_date >= effective]

            if fact.kind == "cancel":
                # The flow has ended. Stop projecting it from the effective date.
                events = [e for e in events if not (e.series == wanted and e.event_date >= effective)]

            elif fact.kind == "amend":
                new_amount = _fact_amount_in_home(
                    fact.new_recurring_amount, fact, home_currency, effective
                )
                if new_amount is None and fact.relative_change and fact.scope_category:
                    # "the renewed lease increases monthly rent by 12%": no
                    # absolute amount to extract, but the obligation is real.
                    # Scoped to the one category the message names, because a
                    # rent rise is not a rise in groceries and transport too.
                    prefix = f"recurring:{wanted}:{fact.scope_category}"
                    for event in matching:
                        if event.source.startswith(prefix):
                            event.amount *= fact.relative_change
                            event.source = f"{event.source}+{fact.message_id}"
                elif new_amount is not None:
                    if matching:
                        signed = new_amount if wanted == "credit" else -new_amount
                        for event in matching:
                            event.amount = signed
                            event.source = f"{event.source}+{fact.message_id}"
                    elif wanted == "credit" and fact.money_is_confirmed_cash:
                        # No detected series to amend. One confirmed credit is
                        # counted, but a recurring series is never manufactured
                        # from a single message.
                        if fact.effective_date and as_of_date < fact.effective_date <= horizon_end:
                            events.append(
                                CashEvent(
                                    fact.effective_date,
                                    new_amount,
                                    f"evidence:{fact.message_id}",
                                    series="credit",
                                )
                            )

        # --- one specific ledger row is cancelled, delayed or corrected ----
        elif target == "related_event" and fact.related_event_id:
            target_id = fact.related_event_id
            if fact.kind == "cancel":
                events = [e for e in events if e.event_id != target_id]
            elif fact.kind == "delay" and fact.effective_date:
                for event in events:
                    if event.event_id == target_id:
                        if fact.effective_date <= horizon_end:
                            event.event_date = fact.effective_date
                        else:
                            events = [e for e in events if e.event_id != target_id]
                            break
            elif fact.kind == "amend":
                revised = _fact_amount_in_home(
                    fact.new_recurring_amount or fact.one_time_amount, fact, home_currency, effective
                )
                if revised is not None:
                    for event in events:
                        if event.event_id == target_id:
                            event.amount = revised if event.amount >= 0 else -revised
            elif not fact.money_is_confirmed_cash:
                # A credit the message says has not actually arrived is removed.
                # A debit is left alone: dropping an obligation would be the
                # unsafe direction.
                events = [
                    e for e in events if not (e.event_id == target_id and e.amount > 0)
                ]

        # --- an extra one-off payment named by the message ------------------
        one_off = _fact_amount_in_home(fact.one_time_amount, fact, home_currency, effective)
        if one_off is not None and fact.one_time_direction in ("credit", "debit"):
            is_credit = fact.one_time_direction == "credit"
            # A credit needs a stated date and explicit confirmation before it
            # is counted (AGENTS.md 6.3: nothing uncertain counts as cash). A
            # debit still needs a stated date, because inventing one would put
            # the obligation at an arbitrary point in the forecast.
            dated = fact.effective_date
            if dated and as_of_date < dated <= horizon_end and (not is_credit or fact.money_is_confirmed_cash):
                already = any(
                    abs((e.event_date - dated).days) <= DEDUP_WINDOW_DAYS
                    and abs(abs(e.amount) - one_off) < 0.01
                    for e in events
                )
                if not already:
                    events.append(
                        CashEvent(
                            dated,
                            one_off if is_credit else -one_off,
                            f"evidence:{fact.message_id}",
                        )
                    )

    return sorted(events, key=lambda e: e.event_date)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconstruct_state(
    user_id: str,
    as_of_date: date,
    horizon_days: int = FORECAST_HORIZON_DAYS,
    evidence: Evidence = EMPTY_EVIDENCE,
) -> FinancialState:
    """Reconstruct user_id's cash position and 90-day forecast as of as_of_date.

    evidence defaults to empty so the forecast stays runnable and testable with
    no network and no API key; passing a populated Evidence adds the image and
    message layer on top of the same deterministic arithmetic.
    """
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

        resolved = _row_amount(row, evidence)
        if resolved is None:
            incomplete_event_ids.append(row["event_id"])
            continue

        raw_amount, currency = resolved
        amount = _convert(raw_amount, currency, home_currency, row["settlement_date"])
        signed = amount if direction == "credit" else -amount
        explicit_events.append(
            CashEvent(settlement, signed, f"ledger:{row['event_id']}", event_id=row["event_id"])
        )

        is_recurring_type = (direction == "debit" and row["event_type"] in DEBIT_RECURRING_TYPES) or (
            direction == "credit" and row["event_type"] in CREDIT_RECURRING_TYPES
        )
        if is_recurring_type:
            key = _series_key(direction, row["category"], row["description"])
            explicit_dates_by_series.setdefault(key, []).append(settlement)

    recurring_events = _detect_recurring_events(
        history,
        home_currency,
        as_of_date,
        horizon_end,
        explicit_dates_by_series,
        incomplete_event_ids,
        evidence,
    )

    forecast_events = _apply_message_facts(
        evidence.for_user(user_id),
        explicit_events + recurring_events,
        as_of_date,
        horizon_end,
        home_currency,
    )

    return FinancialState(
        user_id=user_id,
        as_of_date=as_of_date,
        home_currency=home_currency,
        current_available_balance=float(profile["current_available_balance"]),
        minimum_balance_to_keep=float(profile["minimum_balance_to_keep"]),
        forecast_events=forecast_events,
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
