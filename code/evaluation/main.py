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
- decision_explanation: exact match against the organizers' wording is close to
  meaningless, since many different sentences are equally correct. It is scored
  instead on what the rubric actually asks for, usefulness and consistency,
  measured against our own row rather than against their prose. See
  `_report_explanation_quality`.

Usage:
    python code/evaluation/main.py
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from datetime import date, datetime
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
    return read_rows_by(path, "request_id")


def read_rows_by(path: Path, key: str) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {row[key]: row for row in reader}


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


MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Cue phrases that tie an explanation to one recommended_payment_method. The
# "forbidden" list is the half that matters: an explanation that mentions
# installments on a row recommending a single full payment contradicts the
# column beside it, which the rubric penalises more than vagueness does.
METHOD_CUES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # method: (at least one of these required, none of these allowed)
    "full_payment": (("pay ",), ("installment", "do not", "wait until", "remaining")),
    "installments": (("installment",), ("do not", "wait until", "remaining")),
    "partial_payment": (("remaining",), ("installment", "do not", "wait until")),
    "wait": (("in full on", "wait until"), ("installment", "do not", "today")),
    "not_recommended": (("do not",), ("installment", "wait until")),
}


def _long_dates(text: str) -> set[date]:
    """Every "8 August 2025" style date in the text, as real dates."""
    found = set()
    for day, month, year in re.findall(r"\b(\d{1,2}) ([A-Z][a-z]+) (\d{4})\b", text):
        if month in MONTHS:
            found.add(date(int(year), MONTHS.index(month) + 1, int(day)))
    return found


def _numbers(text: str) -> list[float]:
    """Every money-shaped number in the text, comma grouping removed."""
    out = []
    for token in re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", text):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            pass
    return out


def _report_explanation_quality(
    predicted: dict[str, dict[str, str]],
    source: dict[str, dict[str, str]],
    profiles: dict[str, dict[str, str]],
) -> None:
    """Score decision_explanation on usefulness and consistency, not wording.

    Every rate here is measured against the row we ourselves emitted, so it is a
    real check even on the 250 graded rows where no reference text exists. The
    grounding rate is the one that guards the rule that matters most: a number in
    the sentence that we did not emit anywhere is an invented fact.
    """
    total = len(predicted)
    if not total:
        return

    names_amount = names_currency = names_date = consistent = 0
    names_minimum = grounded = 0

    for request_id, row in predicted.items():
        text = row.get("decision_explanation", "")
        lower = text.lower()
        src = source.get(request_id, {})
        profile = profiles.get(src.get("user_id", ""), {})
        currency = profile.get("home_currency", "")

        # Every number this row is entitled to mention.
        allowed: set[float] = set()
        for key in ("requested_amount", "amount_safe_to_pay"):
            try:
                allowed.add(round(float(row.get(key) or src.get(key) or ""), 2))
            except ValueError:
                pass
        try:
            allowed.add(round(float(profile.get("minimum_balance_to_keep", "")), 2))
        except ValueError:
            pass
        plan_amounts, plan_dates = set(), set()
        payment_count = 0
        for entry in (row.get("payment_plan") or "").split("|"):
            if ":" not in entry:
                continue
            when, _, amount = entry.partition(":")
            try:
                plan_amounts.add(round(float(amount), 2))
                plan_dates.add(datetime.strptime(when, "%Y-%m-%d").date())
                payment_count += 1
            except ValueError:
                pass
        allowed |= plan_amounts
        # "Use 3 installments" is grounded in how many payments the plan has, not
        # in how many distinct amounts it has, which for an even split is always 1.
        allowed.add(float(payment_count))

        allowed_dates = set(plan_dates)
        for key in ("earliest_date_for_full_payment",):
            try:
                allowed_dates.add(datetime.strptime(row.get(key, ""), "%Y-%m-%d").date())
            except ValueError:
                pass
        for key in ("desired_completion_date", "request_date"):
            try:
                allowed_dates.add(datetime.strptime(src.get(key, ""), "%Y-%m-%d").date())
            except ValueError:
                pass

        # Strip anything that is not a money value before grounding, or a day
        # and a year are read as unexplained amounts and every row fails.
        without_dates = re.sub(r"\b\d{1,2} [A-Z][a-z]+ \d{4}\b", " ", text)
        without_dates = re.sub(r"\b90 days\b", " ", without_dates)
        found_numbers = _numbers(without_dates)
        found_dates = _long_dates(text)

        if any(any(abs(n - a) < 0.01 for a in allowed) for n in found_numbers):
            names_amount += 1
        if currency and re.search(rf"\b{currency}\b", text):
            names_currency += 1
        if found_dates & allowed_dates:
            names_date += 1
        try:
            minimum = round(float(profile.get("minimum_balance_to_keep", "")), 2)
            if any(abs(n - minimum) < 0.01 for n in found_numbers):
                names_minimum += 1
        except ValueError:
            pass

        # Grounding: no number in the sentence that this row cannot justify.
        if all(any(abs(n - a) < 0.01 for a in allowed) for n in found_numbers):
            grounded += 1

        required, forbidden = METHOD_CUES.get(
            row.get("recommended_payment_method", ""), ((), ())
        )
        if (not required or any(cue in lower for cue in required)) and not any(
            cue in lower for cue in forbidden
        ):
            consistent += 1

    def rate(label: str, count: int) -> None:
        print(f"    {label:<44} {count:>4}/{total}  ({100 * count / total:5.1f}%)")

    distinct = len({row.get("decision_explanation", "") for row in predicted.values()})
    lengths = [len(row.get("decision_explanation", "")) for row in predicted.values()]
    print(f"\n  decision_explanation quality over {total} row(s)")
    rate("names an amount we emitted", names_amount)
    rate("names the home currency code", names_currency)
    rate("names a date we emitted", names_date)
    rate("names the minimum balance to keep", names_minimum)
    rate("consistent with recommended_payment_method", consistent)
    rate("grounded (no number we did not emit)", grounded)
    print(
        f"    {'distinct strings':<44} {distinct:>4}/{total}"
        f"   avg {sum(lengths) / total:.0f} chars"
    )


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
    _report_explanation_quality(
        predicted, expected, read_rows_by(root / "dataset" / "financial_profiles.csv", "user_id")
    )

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
