"""Tests to prevent regression against the 25 solved sample requests.

Runs the pipeline with --sample (writing to code/evaluation/sample_output.csv,
never touching root output.csv), then scores each output column and overall
accuracy against the ground truth in dataset/sample_requests.csv.

Usage:
    python code/tests/test_score_regression.py
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

# Minimum acceptable accuracy percentages (0.0 to 100.0) for the 25 solved sample rows.
# Note: Raising these thresholds is correct when the pipeline score improves;
# lowering them is forbidden to prevent performance regression.
MIN_SCORE_AMOUNT_SAFE_TO_PAY = 8.0           # 2/25 matches (8.0%)
MIN_SCORE_AFFORDABILITY_STATUS = 64.0        # 16/25 matches (64.0%)
MIN_SCORE_RECOMMENDED_PAYMENT_METHOD = 68.0  # 17/25 matches (68.0%)
MIN_SCORE_PAYMENT_PLAN = 64.0                # 16/25 matches (64.0%)
MIN_SCORE_EARLIEST_DATE = 52.0               # 13/25 matches (52.0%)
MIN_SCORE_SPENDING_CHANGES = 88.0            # 22/25 matches (88.0%)
MIN_SCORE_OVERALL = 57.3                     # 86/150 total matches (57.3%)

AMOUNT_ABS_TOLERANCE = 0.01
AMOUNT_REL_TOLERANCE = 1e-6


def repo_root() -> Path:
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent.parent


def read_rows_by_id(path: Path) -> dict[str, dict[str, str]]:
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


_SCORED_DATA: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]] | None = None


def get_sample_predictions() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    global _SCORED_DATA
    if _SCORED_DATA is not None:
        return _SCORED_DATA

    root = repo_root()
    sample_out = root / "code" / "evaluation" / "sample_output.csv"
    root_out = root / "output.csv"
    root_mtime_before = root_out.stat().st_mtime if root_out.is_file() else None

    # Run the pipeline against sample requests only.
    res = subprocess.run(
        [sys.executable, str(root / "code" / "main.py"), "--sample"],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert res.returncode == 0, f"code/main.py --sample failed:\n{res.stderr}"

    # Critical check: root output.csv must never be modified by sample evaluation.
    root_mtime_after = root_out.stat().st_mtime if root_out.is_file() else None
    assert root_mtime_before == root_mtime_after, "code/main.py --sample modified root output.csv!"

    expected = read_rows_by_id(root / "dataset" / "sample_requests.csv")
    predicted = read_rows_by_id(sample_out)
    assert len(expected) == 25, f"Expected 25 sample requests, got {len(expected)}"
    assert len(predicted) == 25, f"Expected 25 sample predictions, got {len(predicted)}"

    _SCORED_DATA = (expected, predicted)
    return _SCORED_DATA


def _column_score(column: str) -> float:
    expected, predicted = get_sample_predictions()
    matched = 0
    for req_id, exp_row in expected.items():
        pred_row = predicted.get(req_id, {})
        exp_val = exp_row.get(column, "")
        pred_val = pred_row.get(column, "")
        if column == "amount_safe_to_pay":
            ok = amounts_match(exp_val, pred_val)
        else:
            ok = (exp_val.strip() == pred_val.strip())
        matched += int(ok)
    return round(100.0 * matched / len(expected), 1)


def test_score_amount_safe_to_pay():
    score = _column_score("amount_safe_to_pay")
    assert score >= MIN_SCORE_AMOUNT_SAFE_TO_PAY, (
        f"amount_safe_to_pay score {score}% is below baseline {MIN_SCORE_AMOUNT_SAFE_TO_PAY}%"
    )


def test_score_affordability_status():
    score = _column_score("affordability_status")
    assert score >= MIN_SCORE_AFFORDABILITY_STATUS, (
        f"affordability_status score {score}% is below baseline {MIN_SCORE_AFFORDABILITY_STATUS}%"
    )


def test_score_recommended_payment_method():
    score = _column_score("recommended_payment_method")
    assert score >= MIN_SCORE_RECOMMENDED_PAYMENT_METHOD, (
        f"recommended_payment_method score {score}% is below baseline {MIN_SCORE_RECOMMENDED_PAYMENT_METHOD}%"
    )


def test_score_payment_plan():
    score = _column_score("payment_plan")
    assert score >= MIN_SCORE_PAYMENT_PLAN, (
        f"payment_plan score {score}% is below baseline {MIN_SCORE_PAYMENT_PLAN}%"
    )


def test_score_earliest_date_for_full_payment():
    score = _column_score("earliest_date_for_full_payment")
    assert score >= MIN_SCORE_EARLIEST_DATE, (
        f"earliest_date_for_full_payment score {score}% is below baseline {MIN_SCORE_EARLIEST_DATE}%"
    )


def test_score_spending_changes_needed():
    score = _column_score("spending_changes_needed")
    assert score >= MIN_SCORE_SPENDING_CHANGES, (
        f"spending_changes_needed score {score}% is below baseline {MIN_SCORE_SPENDING_CHANGES}%"
    )


def test_score_overall():
    expected, predicted = get_sample_predictions()
    columns = [
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
    ]
    total_matches = 0
    for col in columns:
        for req_id, exp_row in expected.items():
            pred_row = predicted.get(req_id, {})
            exp_val = exp_row.get(col, "")
            pred_val = pred_row.get(col, "")
            if col == "amount_safe_to_pay":
                ok = amounts_match(exp_val, pred_val)
            else:
                ok = (exp_val.strip() == pred_val.strip())
            total_matches += int(ok)
    overall_score = round(100.0 * total_matches / (len(columns) * len(expected)), 1)
    assert overall_score >= MIN_SCORE_OVERALL, (
        f"overall score {overall_score}% is below baseline {MIN_SCORE_OVERALL}%"
    )


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
