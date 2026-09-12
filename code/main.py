"""Buy or Wait? - entry point.

Reads dataset/requests.csv, writes output.csv at the repo root with one row
per request in the required column order. This step is plumbing only: every
row gets a safe placeholder prediction, no financial decision logic yet.

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
from pathlib import Path

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

# Placeholder prediction: safe (amount 0, no payment recommended) and valid
# against every enum in the output contract, so the gates pass before any
# decision logic exists.
PLACEHOLDER = {
    "amount_safe_to_pay": "0",
    "affordability_status": "not_affordable",
    "recommended_payment_method": "not_recommended",
    "payment_plan": "none",
    "earliest_date_for_full_payment": "",
    "spending_changes_needed": "none",
    "decision_explanation": "Placeholder: decision logic not yet implemented.",
}


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def read_request_ids(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [row["request_id"] for row in reader]


def write_output(path: Path, request_ids: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        for request_id in request_ids:
            writer.writerow({"request_id": request_id, **PLACEHOLDER})


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

    request_ids = read_request_ids(input_path)
    write_output(output_path, request_ids)

    print(f"read {len(request_ids)} request(s) from {input_path}")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
