"""Eligibility and plan selection: affordability_status and recommended_payment_method.

These two columns are very nearly one column. Across the 25 solved samples only
six (status, method) pairs occur, and three of them are one-to-one. So status is
decided first and the method follows from it, with a real choice only inside
`affordable_with_plan`.

Design constraint, deliberate: this module consumes the forecast as a
RELATIONSHIP, not as a precise number. `code/evaluation/diagnose_trough.py`
measured our trough against the organizers' and found a median error of about
5% of one month's recurring spend on well-behaved rows, and far worse on rows
whose trough falls late in the horizon (DECISIONS.md, D9). Betting these
columns on the second decimal of `amount_safe_to_pay` would inherit an accuracy
we do not have. What is used instead:

- `coverage`, the ratio amount_safe_to_pay / requested_amount. A ratio survives
  a proportional error in the trough that an absolute threshold would not.
- whether a full payment is ever safe inside the horizon, which is a yes/no.
- whether each payment of a candidate plan clears the minimum balance, which is
  a comparison rather than a quantity.

`not_affordable` is therefore right whenever the balance is clearly short,
whether the safe amount comes out at 872,453 or 873,000.

Eligibility is not the same as availability. An option listed in
`request_payment_options.csv` is only usable if the user's own preferences allow
it: 119 of 275 users have a blank `max_installment_months` and will not consider
installments at all, and `allows_partial_payment` is true for only 80 of 250
requests.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from financial_state import (
    FinancialState,
    _balance_timeline,
    amount_safe_to_pay,
    earliest_date_for_full_payment,
    repo_root,
)

# Coverage at or above this counts as the whole request being safe today. Not
# 1.0 exactly, because money arithmetic carries rounding.
FULL_COVERAGE = 0.999

# A shortfall smaller than this is treated as closable by permitted spending
# changes rather than as an inability to pay. Derived from the observed boundary
# in the solved samples: every `affordable_with_plan` row whose method is
# `full_payment` sits at coverage 0.95 or above (0.95, 0.97, 0.98), and every
# `affordable_later` row sits at 0.66 or below. The gap between those two
# clusters is wide, which is what makes a band usable here at all.
NEAR_MISS_COVERAGE = 0.95

# Money comparisons are made with a small tolerance so a repeating-decimal
# instalment split does not read as a breach of the minimum balance.
EPSILON = 0.01


@dataclass
class Plan:
    """One candidate way of completing the request."""

    method: str                              # full_payment | partial_payment | installments
    payments: list[tuple[date, float]] = field(default_factory=list)
    total_cost: float = 0.0                  # what the user pays in total, fees included
    option_id: str = ""
    completes_by_deadline: bool = True

    @property
    def payment_count(self) -> int:
        return len(self.payments)

    @property
    def starts_on(self) -> date | None:
        return self.payments[0][0] if self.payments else None


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_options_by_request() -> dict[str, list[dict]]:
    root = repo_root()
    by_request: dict[str, list[dict]] = {}
    path = root / "dataset" / "request_payment_options.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_request.setdefault(row["request_id"], []).append(row)
    return by_request


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _as_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value: str, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def accepted_methods(profile: dict) -> set[str]:
    raw = (profile.get("payment_methods_user_will_consider") or "").strip()
    return {part.strip() for part in raw.split("|") if part.strip()}


def max_installments(profile: dict) -> int:
    """0 means the user will not consider installments at all (blank field)."""
    return _as_int(profile.get("max_installment_months", ""), 0)


def balance_at(state: FinancialState, when: date) -> float:
    """Projected balance on `when`, from the forecast timeline.

    Past the horizon the last projected balance is carried forward: there is no
    forecast beyond it, and inventing further income or expense is exactly what
    AGENTS.md section 6.3 forbids.
    """
    balance = state.current_available_balance
    for point_date, point_balance in _balance_timeline(state):
        if point_date <= when:
            balance = point_balance
        else:
            break
    return balance


def plan_is_safe(state: FinancialState, payments: list[tuple[date, float]]) -> bool:
    """True if no payment in the plan ever pushes the projection below the floor.

    Checked at every forecast point and at every payment date, because a payment
    can fall between two forecast points and a dip can fall between two payments.
    """
    if not payments:
        return False
    checkpoints = {point_date for point_date, _ in _balance_timeline(state)}
    checkpoints.update(payment_date for payment_date, _ in payments)
    for when in sorted(checkpoints):
        paid = sum(amount for payment_date, amount in payments if payment_date <= when)
        if balance_at(state, when) - paid < state.minimum_balance_to_keep - EPSILON:
            return False
    return True


def _installment_payments(option: dict, fallback_start: date) -> list[tuple[date, float]]:
    start = _parse_date(option.get("first_payment_date", "")) or fallback_start
    count = max(_as_int(option.get("number_of_payments", ""), 0), 0)
    amount = _as_float(option.get("payment_amount", ""))
    gap = max(_as_int(option.get("payment_frequency_days", ""), 30), 1)
    return [(start + timedelta(days=gap * i), amount) for i in range(count)]


def eligible_plans(
    request: dict, profile: dict, state: FinancialState, requested_amount: float
) -> list[Plan]:
    """Every plan that the user's preferences allow AND the forecast can carry.

    Availability in request_payment_options.csv is necessary, not sufficient.
    """
    methods = accepted_methods(profile)
    request_date = state.as_of_date
    deadline = _parse_date(request.get("desired_completion_date", ""))
    safe_today = amount_safe_to_pay(state, requested_amount)
    earliest = earliest_date_for_full_payment(state, requested_amount)
    plans: list[Plan] = []

    # Installments: the schedule must come from a supplied option, and the
    # number of payments must be within what the user will consider.
    if "installments" in methods:
        cap = max_installments(profile)
        for option in _load_options_by_request().get(request["request_id"], []):
            if option.get("payment_method") != "installments":
                continue
            count = _as_int(option.get("number_of_payments", ""), 0)
            if cap <= 0 or count <= 0 or count > cap:
                continue
            payments = _installment_payments(option, request_date)
            if not plan_is_safe(state, payments):
                continue
            last = payments[-1][0]
            plans.append(
                Plan(
                    method="installments",
                    payments=payments,
                    total_cost=_as_float(
                        option.get("total_payable_amount", ""), requested_amount
                    ),
                    option_id=option.get("payment_option_id", ""),
                    completes_by_deadline=deadline is None or last <= deadline,
                )
            )

    # Partial payment: exactly two payments, the safe amount now and the balance
    # on the first date a full payment would have been safe. Only when the
    # request permits it, the user accepts it, the split is a real split, and
    # the second payment lands on or before the deadline.
    if (
        "partial_payment" in methods
        and (request.get("allows_partial_payment", "").strip().lower() == "true")
        and earliest is not None
        and earliest > request_date
        and EPSILON < safe_today < requested_amount - EPSILON
        and (deadline is None or earliest <= deadline)
    ):
        payments = [(request_date, safe_today), (earliest, requested_amount - safe_today)]
        plans.append(
            Plan(
                method="partial_payment",
                payments=payments,
                total_cost=requested_amount,      # a split carries no financing fee
                completes_by_deadline=True,
            )
        )

    return plans


def rank_plans(plans: list[Plan]) -> list[Plan]:
    """problem_statement.md section 6.3, in order: complete by the deadline, then
    minimise what is actually paid, then start earlier, then use fewer payments.

    Cost is what separates the two plan types in practice. A partial split has no
    financing fee, so where both a split and an instalment option are eligible the
    split wins on total paid, which is what the solved samples show.
    """
    return sorted(
        plans,
        key=lambda plan: (
            not plan.completes_by_deadline,
            round(plan.total_cost, 2),
            plan.starts_on or date.max,
            plan.payment_count,
            plan.option_id,
        ),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    request: dict, profile: dict, state: FinancialState, requested_amount: float
) -> tuple[str, str, Plan | None]:
    """(affordability_status, recommended_payment_method, chosen plan or None)."""
    methods = accepted_methods(profile)
    safe_today = amount_safe_to_pay(state, requested_amount)
    earliest = earliest_date_for_full_payment(state, requested_amount)
    coverage = safe_today / requested_amount if requested_amount > 0 else 1.0

    # 1. The whole request is safe today and the user will pay in full.
    #    Acceptance matters: a user who only considers installments is not
    #    "affordable_now" however healthy the balance looks.
    if coverage >= FULL_COVERAGE and "full_payment" in methods:
        return "affordable_now", "full_payment", Plan(
            method="full_payment",
            payments=[(state.as_of_date, requested_amount)],
            total_cost=requested_amount,
        )

    # 2. A real plan completes the whole request. Preferred over spending
    #    changes, which the ranking rules treat as a last resort.
    ranked = rank_plans(eligible_plans(request, profile, state, requested_amount))
    if ranked:
        return "affordable_with_plan", ranked[0].method, ranked[0]

    # 3. A near miss: short by little enough that permitted spending changes can
    #    close it, and the user is willing to pay in full.
    if coverage >= NEAR_MISS_COVERAGE and "full_payment" in methods:
        return "affordable_with_plan", "full_payment", Plan(
            method="full_payment",
            payments=[(state.as_of_date, requested_amount)],
            total_cost=requested_amount,
        )

    # 4. No plan now, but capacity arrives inside the forecast: wait for it.
    if earliest is not None:
        return "affordable_later", "wait", None

    # 5. A safe full payment never happens inside the horizon and no eligible
    #    plan carries it. Do not proceed.
    return "not_affordable", "not_recommended", None
