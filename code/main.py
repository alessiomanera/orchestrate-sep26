"""Buy or Wait? - entry point.

Reads dataset/requests.csv, writes output.csv at the repo root with one row
per request in the required column order.

amount_safe_to_pay and earliest_date_for_full_payment come from the
deterministic 90-day forecast in code/financial_state.py. The remaining four
columns (affordability_status, recommended_payment_method, payment_plan,
spending_changes_needed) are still a safe placeholder: eligibility and plan
ranking have not been built yet.

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

from financial_state import amount_safe_to_pay as compute_amount_safe_to_pay
from financial_state import earliest_date_for_full_payment as compute_earliest_date
from financial_state import reconstruct_state

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

# Placeholder prediction for the four columns eligibility and plan ranking
# have not been built for yet: safe (no payment recommended) and valid
# against every enum in the output contract.
PLACEHOLDER = {
    "affordability_status": "not_affordable",
    "recommended_payment_method": "not_recommended",
    "payment_plan": "none",
    "spending_changes_needed": "none",
    "decision_explanation": "Placeholder: decision logic not yet implemented.",
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


def predict(request: dict) -> dict:
    """amount_safe_to_pay and earliest_date_for_full_payment from the 90-day
    forecast; every other column is still the safe placeholder.
    """
    request_date = datetime.strptime(request["request_date"], "%Y-%m-%d").date()
    requested_amount = float(request["requested_amount"])
    state = reconstruct_state(request["user_id"], request_date)

    safe_amount = compute_amount_safe_to_pay(state, requested_amount)
    earliest = compute_earliest_date(state, requested_amount)

    return {
        "request_id": request["request_id"],
        "amount_safe_to_pay": _format_amount(safe_amount),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        **PLACEHOLDER,
    }


def write_output(path: Path, requests: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        for request in requests:
            writer.writerow(predict(request))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="run against dataset/sample_requests.csv instead of dataset/requests.csv",
    )
    args = parser.parse_args()

    root = repo_root()
    if args.sample:
        input_path = root / "dataset" / "sample_requests.csv"
        output_path = root / "code" / "evaluation" / "sample_output.csv"
    else:
        input_path = root / "dataset" / "requests.csv"
        output_path = root / "output.csv"

    requests = read_requests(input_path)
    write_output(output_path, requests)

    print(f"read {len(requests)} request(s) from {input_path}")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
