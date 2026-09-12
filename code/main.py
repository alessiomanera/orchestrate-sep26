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
from datetime import datetime
from pathlib import Path

from evidence import EMPTY_EVIDENCE, Evidence, collect_evidence
from financial_state import _load_profiles
from financial_state import amount_safe_to_pay as compute_amount_safe_to_pay
from financial_state import earliest_date_for_full_payment as compute_earliest_date
from financial_state import reconstruct_state
from planning import classify, format_payment_plan
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

EXPLANATION = {
    "affordable_now": "The full amount clears the minimum balance on the request date.",
    "affordable_with_plan": "The full amount is not safe today, but an eligible plan completes it.",
    "affordable_later": "No eligible plan completes this now; capacity arrives later in the forecast.",
    "not_affordable": "No safe full payment occurs within the forecast and no eligible plan carries it.",
}


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

    explanation = EXPLANATION[status]
    if plan is not None and plan.method == "installments":
        explanation += f" Chosen option {plan.option_id}: {plan.payment_count} payments."
    elif plan is not None and plan.method == "partial_payment":
        explanation += " A two-part split completes it by the desired date at no financing cost."

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
