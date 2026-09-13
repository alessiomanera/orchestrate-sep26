"""Buy or Wait? - entry point.

Reads dataset/requests.csv, writes output.csv at the repo root with one row
per request in the required column order.

amount_safe_to_pay and earliest_date_for_full_payment come from the
deterministic 90-day forecast in code/financial_state.py, corrected by the
evidence layer in code/evidence.py (the 16 document images and the 215
messages). affordability_status and recommended_payment_method come from the
eligibility filter and plan ranking in code/planning.py. payment_plan and
spending_changes_needed are still safe placeholders: the plan grammar and the
spending-change selector are the next step.

Usage:
    python code/main.py              # dataset/requests.csv -> output.csv
    python code/main.py --sample     # dataset/sample_requests.csv -> code/evaluation/sample_output.csv

--sample targets the 25 solved rows in sample_requests.csv, which is how we
self-score before the real 250-row run. It never touches the root output.csv:
that file must only ever be produced by the real 250-row run, since it is the
one submitted to HackerRank.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from pathlib import Path

from evidence import EMPTY_EVIDENCE, Evidence, collect_evidence
from financial_state import _load_profiles
from financial_state import amount_safe_to_pay as compute_amount_safe_to_pay
from financial_state import earliest_date_for_full_payment as compute_earliest_date
from financial_state import reconstruct_state
from planning import Plan, classify, format_payment_plan
from usage import write_usage_report

REQUIRED_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

# spending_changes_needed is still a safe placeholder: the selector is the next
# step, and "none" is valid against the output contract.
PLACEHOLDER = {
    "spending_changes_needed": "none",
}

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _money(currency: str, value: float) -> str:
    """"EUR 1,574.40". Comma grouping, and decimals only when there are any.

    Read off the solved rows, which write 25,256 and 68,432 without decimals but
    620.40, 23.50 and 15,952,906.67 with exactly two. Note this is the grammar of
    the prose column only: payment_plan uses no grouping and
    earliest_date_for_full_payment stays ISO, so the three are formatted apart
    rather than sharing one helper.
    """
    rounded = round(value, 2)
    body = f"{int(rounded):,}" if rounded == int(rounded) else f"{rounded:,.2f}"
    return f"{currency} {body}".strip()


def _long_date(value: date) -> str:
    """"15 November 2019". The solved explanations use long form even though
    payment_plan on the same row uses ISO, so the two deliberately differ.
    """
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def _parse_date(value: str) -> date | None:
    """An optional ISO date from the request row, or None when it is blank."""
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _plan_entries(plan: Plan | None) -> list[tuple[date, float]]:
    return sorted(plan.payments, key=lambda p: p[0]) if plan and plan.payments else []


def build_explanation(
    status: str,
    method: str,
    plan: Plan | None,
    *,
    currency: str,
    minimum: float,
    requested: float,
    earliest: date | None,
    deadline: date | None,
) -> str:
    """A grounded two-sentence explanation: the action, then the safety reason.

    Every number and date here is one this row actually emits, or is read
    directly from the profile or the request. Nothing is inferred and nothing is
    estimated, so the sentence can never contradict the seven columns beside it.
    Where a value is unknown the sentence says less rather than guessing, which
    is why several branches drop their second sentence instead of inventing a
    reason.

    Shape derived from all 25 solved rows, not from a sample of them: sentence
    one names the recommended action with its amount and date, sentence two says
    what the minimum balance does under that plan.
    """
    entries = _plan_entries(plan)
    floor = f"This leaves at least {_money(currency, minimum)} available."

    if method == "full_payment":
        # affordable_now and the near-miss branch both pay the full amount on the
        # request date, so they read the same. We emit spending_changes_needed as
        # "none", so no spending change is ever claimed here.
        return (
            f"Pay {_money(currency, requested)} today. This leaves at least "
            f"{_money(currency, minimum)} available over the next 90 days."
        )

    if method == "installments" and entries:
        amounts = {round(a, 2) for _, a in entries}
        start = _long_date(entries[0][0])
        if len(amounts) == 1:
            head = (
                f"Use {len(entries)} installments of "
                f"{_money(currency, entries[0][1])}, starting {start}."
            )
        else:
            total = sum(a for _, a in entries)
            head = (
                f"Use {len(entries)} installments totalling "
                f"{_money(currency, total)}, starting {start}."
            )
        return f"{head} {floor}"

    if method == "partial_payment" and len(entries) == 2:
        return (
            f"Pay {_money(currency, entries[0][1])} today and the remaining "
            f"{_money(currency, entries[1][1])} on {_long_date(entries[1][0])}. "
            f"This completes the full request and keeps the "
            f"{_money(currency, minimum)} minimum protected."
        )

    if method == "wait" and entries:
        return (
            f"Pay {_money(currency, requested)} in full on "
            f"{_long_date(entries[0][0])}. Paying earlier would take the balance "
            f"below the {_money(currency, minimum)} minimum."
        )

    if method == "not_recommended":
        if earliest is not None:
            # Capacity does arrive, so it would be false to say no option keeps
            # the minimum safe. The blocker is the user's stated preference.
            return (
                f"Do not proceed with the {_money(currency, requested)} request. "
                f"The full amount would be safe from {_long_date(earliest)}, but "
                f"full payment is not a method this user accepts."
            )
        if deadline is not None:
            return (
                f"Do not make this payment by {_long_date(deadline)}. None of the "
                f"available options keeps the {_money(currency, minimum)} minimum "
                f"protected."
            )
        return (
            f"Do not proceed with the {_money(currency, requested)} request. The "
            f"full amount cannot be completed safely within 90 days while keeping "
            f"the {_money(currency, minimum)} minimum protected."
        )

    # No plan was built for a method that needs one. Say the little that is known
    # rather than describe a schedule that does not exist.
    return (
        f"No safe plan completes the {_money(currency, requested)} request while "
        f"keeping the {_money(currency, minimum)} minimum protected."
    )


def _format_amount(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def read_requests(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def predict(request: dict, evidence: Evidence) -> dict:
    """Five live columns from the forecast, the eligibility filter and the plan
    grammar; spending_changes_needed is still a placeholder.
    """
    request_date = datetime.strptime(request["request_date"], "%Y-%m-%d").date()
    requested_amount = float(request["requested_amount"])
    state = reconstruct_state(request["user_id"], request_date, evidence=evidence)
    profile = _load_profiles()[request["user_id"]]

    safe_amount = compute_amount_safe_to_pay(state, requested_amount)
    earliest = compute_earliest_date(state, requested_amount)
    status, method, plan = classify(request, profile, state, requested_amount)

    explanation = build_explanation(
        status,
        method,
        plan,
        currency=profile.get("home_currency", ""),
        minimum=float(profile["minimum_balance_to_keep"]),
        requested=requested_amount,
        earliest=earliest,
        deadline=_parse_date(request.get("desired_completion_date", "")),
    )

    return {
        "request_id": request["request_id"],
        "amount_safe_to_pay": _format_amount(safe_amount),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": format_payment_plan(plan),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "decision_explanation": explanation,
        **PLACEHOLDER,
    }


def write_output(path: Path, requests: list[dict], evidence: Evidence) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        for request in requests:
            writer.writerow(predict(request, evidence))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="run against dataset/sample_requests.csv instead of dataset/requests.csv",
    )
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="skip the image and message layer (ablation: forecast from the ledger alone)",
    )
    args = parser.parse_args()

    root = repo_root()
    if args.sample:
        input_path = root / "dataset" / "sample_requests.csv"
        output_path = root / "code" / "evaluation" / "sample_output.csv"
    else:
        input_path = root / "dataset" / "requests.csv"
        output_path = root / "output.csv"

    evidence = EMPTY_EVIDENCE if args.no_evidence else collect_evidence()

    requests = read_requests(input_path)
    write_output(output_path, requests, evidence)

    print(f"read {len(requests)} request(s) from {input_path}")
    print(f"wrote {output_path}")

    # The usage report must describe the graded full-dataset run, so a --sample
    # run never overwrites it with 25-row figures.
    if not args.no_evidence and not args.sample:
        print(f"wrote {write_usage_report(len(requests))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
