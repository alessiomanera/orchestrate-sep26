"""Self-scoring harness for the 25 solved rows in dataset/sample_requests.csv.

Runs the pipeline (code/main.py --sample) to regenerate predictions, then
scores each output column against the expected answers already present in
sample_requests.csv (columns 9 to 15: amount_safe_to_pay, affordability_status,
recommended_payment_method, payment_plan, earliest_date_for_full_payment,
spending_changes_needed, decision_explanation). This is how the pipeline is
self-scored before it is trusted against the real 250-row dataset/requests.csv.

Scoring rules:
- affordability_status, recommended_payment_method, payment_plan,
  earliest_date_for_full_payment, spending_changes_needed: exact string match.
- amount_safe_to_pay: numeric comparison with a small tolerance, since plan
  arithmetic can produce repeating-decimal rounding.
- decision_explanation: not scored for correctness. Only reported as a
  non-empty rate.

Usage:
    python code/evaluation/main.py
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

EXACT_MATCH_COLUMNS = [
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
]
NUMERIC_TOLERANCE_COLUMN = "amount_safe_to_pay"
NON_EMPTY_ONLY_COLUMN = "decision_explanation"

# Money values can carry repeating-decimal rounding (e.g. an installment split
# three ways), so amounts are compared within a small tolerance rather than
# exactly.
AMOUNT_ABS_TOLERANCE = 0.01
AMOUNT_REL_TOLERANCE = 1e-6


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent.parent


def read_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {row["request_id"]: row for row in reader}


def amounts_match(expected: str, actual: str) -> bool:
    try:
        e, a = float(expected), float(actual)
    except ValueError:
        return expected.strip() == actual.strip()
    diff = abs(e - a)
    return diff <= AMOUNT_ABS_TOLERANCE or diff <= AMOUNT_REL_TOLERANCE * max(abs(e), abs(a))


def _report_amount_error_split(
    expected: dict[str, dict[str, str]], predicted: dict[str, dict[str, str]]
) -> None:
    """Direction of the error on amount_safe_to_pay, not just its hit rate.

    Exact-match percentage says how often we are right; it says nothing about
    how we are wrong. A one-sided split means a systematically missing or
    double-counted obligation. A balanced split means errors in both directions
    at once, which needs a different explanation. This is the diagnostic the
    evidence-layer decision was made on (DECISIONS.md, D2), so it is reported
    on every run rather than reconstructed by hand when it is needed.
    """
    too_high = too_low = exact = 0
    for request_id, expected_row in expected.items():
        predicted_row = predicted.get(request_id, {})
        try:
            difference = float(predicted_row.get(NUMERIC_TOLERANCE_COLUMN, "")) - float(
                expected_row.get(NUMERIC_TOLERANCE_COLUMN, "")
            )
        except ValueError:
            continue
        if abs(difference) <= AMOUNT_ABS_TOLERANCE:
            exact += 1
        elif difference > 0:
            too_high += 1
        else:
            too_low += 1

    scored = too_high + too_low + exact
    skew = abs(too_high - too_low) / scored if scored else 0.0
    print(
        f"\n  {NUMERIC_TOLERANCE_COLUMN} error direction: "
        f"{too_high} too high, {too_low} too low, {exact} exact "
        f"(skew {skew:.0%}; 0% is perfectly symmetric)"
    )


def _report_confusion(
    expected: dict[str, dict[str, str]],
    predicted: dict[str, dict[str, str]],
    column: str,
) -> None:
    """Confusion matrix for a categorical column.

    A single accuracy number hides which way a classifier leans. On these two
    columns that matters more than usual: the placeholder that preceded this
    logic scored 28% purely by always guessing the most common class, so the
    shape of the errors says whether real discrimination has been gained.
    """
    labels = sorted(
        {row.get(column, "") for row in expected.values()}
        | {row.get(column, "") for row in predicted.values()}
    )
    width = max(len(label) for label in labels) + 2
    print(f"\n  Confusion for {column} (rows = truth, columns = predicted)")
    header = " " * (width + 2) + "".join(f"{label[:12]:>14}" for label in labels)
    print(header)
    for truth in labels:
        cells = []
        for guess in labels:
            count = sum(
                1
                for request_id, expected_row in expected.items()
                if expected_row.get(column, "") == truth
                and predicted.get(request_id, {}).get(column, "") == guess
            )
            cells.append(f"{count if count else '.':>14}")
        total = sum(1 for row in expected.values() if row.get(column, "") == truth)
        print(f"  {truth:<{width}}" + "".join(cells) + f"   (n={total})")


def main() -> int:
    root = repo_root()

    # Regenerate predictions rather than trust a stale sample_output.csv.
    subprocess.run(
        [sys.executable, str(root / "code" / "main.py"), "--sample"],
        cwd=str(root),
        check=True,
    )

    expected = read_rows(root / "dataset" / "sample_requests.csv")
    predicted = read_rows(root / "code" / "evaluation" / "sample_output.csv")
    total = len(expected)

    print(f"Scoring {total} row(s) from dataset/sample_requests.csv\n")

    scored_columns = [NUMERIC_TOLERANCE_COLUMN, *EXACT_MATCH_COLUMNS]
    column_matches: dict[str, int] = {}

    for column in scored_columns:
        matched = 0
        for request_id, exp_row in expected.items():
            pred_row = predicted.get(request_id, {})
            exp_val = exp_row.get(column, "")
            pred_val = pred_row.get(column, "")
            if column == NUMERIC_TOLERANCE_COLUMN:
                ok = amounts_match(exp_val, pred_val)
            else:
                ok = exp_val.strip() == pred_val.strip()
            matched += int(ok)
        column_matches[column] = matched
        pct = 100 * matched / total if total else 0.0
        print(f"  {column:<28} {matched:>3}/{total}  ({pct:5.1f}%)")

    non_empty = sum(
        1
        for request_id in expected
        if predicted.get(request_id, {}).get(NON_EMPTY_ONLY_COLUMN, "").strip()
    )
    pct_non_empty = 100 * non_empty / total if total else 0.0
    print(
        f"  {NON_EMPTY_ONLY_COLUMN:<28} {non_empty:>3}/{total}  ({pct_non_empty:5.1f}%)"
        "  non-empty only, not scored for correctness"
    )

    _report_amount_error_split(expected, predicted)
    _report_confusion(expected, predicted, "affordability_status")
    _report_confusion(expected, predicted, "recommended_payment_method")

    overall_matched = sum(column_matches.values())
    overall_total = len(scored_columns) * total
    overall_pct = 100 * overall_matched / overall_total if overall_total else 0.0
    print(
        f"\nOverall across {len(scored_columns)} scored columns "
        f"(excludes {NON_EMPTY_ONLY_COLUMN}): "
        f"{overall_matched}/{overall_total} ({overall_pct:5.1f}%)"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
