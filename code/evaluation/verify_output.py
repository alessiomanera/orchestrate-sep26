"""Independent validator checking output.csv against the specification (15 rules).

Verifies output.csv rows against problem_statement.md and dataset files:
- dataset/requests.csv
- dataset/financial_profiles.csv
- dataset/request_payment_options.csv
- dataset/financial_events.csv

Checks 15 rules:
1.  Exactly the 8 required columns, in the required order, no extras.
2.  One row per request_id in dataset/requests.csv, in that file's own row order, 250 rows.
3.  No required field empty, except earliest_date_for_full_payment which may be empty.
4.  0 <= amount_safe_to_pay <= requested_amount, on every row.
5.  affordability_status is one of affordable_now, affordable_with_plan, affordable_later,
    not_affordable.
6.  recommended_payment_method is one of full_payment, partial_payment, installments, wait,
    not_recommended.
7.  If affordability_status is affordable_now then earliest_date_for_full_payment equals
    request_date.
8.  If recommended_payment_method is full_payment, partial_payment or installments, then that
    method appears in the user's payment_methods_user_will_consider.
9.  If recommended_payment_method is wait, then full_payment appears in
    payment_methods_user_will_consider.
10. If recommended_payment_method is partial_payment then all of: the request has
    allows_partial_payment true; affordability_status is affordable_with_plan;
    0 < amount_safe_to_pay < requested_amount; payment_plan has exactly two payments; the
    first is amount_safe_to_pay dated request_date; the two amounts sum to requested_amount;
    the second payment date is on or before desired_completion_date.
11. If recommended_payment_method is installments then payment_plan must exactly match one
    supplied option in request_payment_options.csv for that request_id: same number of
    payments, same first payment date, and dates spaced by that option's
    payment_frequency_days.
12. payment_plan is either the literal none or chronological entries of the form
    YYYY-MM-DD:amount joined by the pipe character, dates non-decreasing, all amounts
    positive and parseable.
13. If recommended_payment_method is not_recommended then payment_plan is none.
14. spending_changes_needed is either the literal none or at most three entries joined by the
    pipe character, each either stop:<event_id> or reduce_to:<event_id>:<amount>. Every
    event_id must exist in financial_events.csv for that request's user. Every referenced
    event must be a recurring expense whose flexibility is not fixed. The same event_id must
    not appear in both a stop and a reduce_to. For reduce_to, the new amount must be greater
    than or equal to that event's minimum_allowed_amount where one is present.
15. decision_explanation is non-empty on every row.

Usage:
    python code/evaluation/verify_output.py
    python code/evaluation/verify_output.py --output output.csv
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys

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

ALLOWED_STATUSES = {
    "affordable_now",
    "affordable_with_plan",
    "affordable_later",
    "not_affordable",
}

ALLOWED_PAYMENT_METHODS = {
    "full_payment",
    "partial_payment",
    "installments",
    "wait",
    "not_recommended",
}


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent.parent


def load_dataset(dataset_dir: Path) -> tuple[
    list[dict[str, str]],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
    dict[str, list[dict[str, str]]],
    dict[str, dict[str, dict[str, str]]],
]:
    """Load supporting dataset tables into lookup structures."""
    requests_rows: list[dict[str, str]] = []
    requests_by_id: dict[str, dict[str, str]] = {}
    with (dataset_dir / "requests.csv").open(newline="", encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            requests_rows.append(r)
            requests_by_id[r["request_id"]] = r

    profiles_by_user: dict[str, dict[str, str]] = {}
    with (dataset_dir / "financial_profiles.csv").open(newline="", encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            profiles_by_user[r["user_id"]] = r

    options_by_request: dict[str, list[dict[str, str]]] = {}
    with (dataset_dir / "request_payment_options.csv").open(newline="", encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            options_by_request.setdefault(r["request_id"], []).append(r)

    user_events: dict[str, dict[str, dict[str, str]]] = {}
    with (dataset_dir / "financial_events.csv").open(newline="", encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            user_events.setdefault(r["user_id"], {})[r["event_id"]] = r

    return requests_rows, requests_by_id, profiles_by_user, options_by_request, user_events


def verify_output(
    output_path: Path,
    dataset_dir: Path,
) -> tuple[bool, list[tuple[int, str, list[str]]]]:
    """Verify output.csv against all 15 rules.

    Returns:
        (all_passed, [(rule_num, description, list_of_violating_request_ids_or_reasons)])
    """
    if not output_path.is_file():
        return False, [(0, "output.csv existence", [f"File not found: {output_path}"])]

    (
        requests_rows,
        requests_by_id,
        profiles_by_user,
        options_by_request,
        user_events,
    ) = load_dataset(dataset_dir)

    with output_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        raw_rows = list(reader)

    header = raw_rows[0] if raw_rows else []
    output_rows = [dict(zip(header, r)) for r in raw_rows[1:] if r]

    results: list[tuple[int, str, list[str]]] = []

    # Rule 1: Exactly the 8 required columns, in the required order, no extras.
    r1_violations: list[str] = []
    if header != REQUIRED_COLUMNS:
        r1_violations.append(f"Header mismatch: expected {REQUIRED_COLUMNS}, got {header}")
    for idx, row in enumerate(raw_rows[1:], start=1):
        if len(row) != len(header):
            rid = row[0] if row else f"row_{idx}"
            r1_violations.append(rid)
    results.append((1, "Exactly 8 required columns in exact order", r1_violations))

    # Rule 2: One row per request_id in dataset/requests.csv, in that file's own row order, 250 rows.
    r2_violations: list[str] = []
    expected_ids = [r["request_id"] for r in requests_rows]
    actual_ids = [r.get("request_id", "") for r in output_rows]
    if len(actual_ids) != len(expected_ids):
        r2_violations.append(f"Row count mismatch: expected {len(expected_ids)}, got {len(actual_ids)}")
    for exp_id, act_id in zip(expected_ids, actual_ids):
        if exp_id != act_id:
            r2_violations.append(f"Expected {exp_id}, got {act_id}")
    if len(actual_ids) > len(expected_ids):
        for extra in actual_ids[len(expected_ids):]:
            r2_violations.append(f"Extra row: {extra}")
    elif len(expected_ids) > len(actual_ids):
        for missing in expected_ids[len(actual_ids):]:
            r2_violations.append(f"Missing row: {missing}")
    results.append((2, "One row per request_id in requests.csv row order (250 rows)", r2_violations))

    # Rule 3: No required field empty, except earliest_date_for_full_payment which may be empty.
    r3_violations: list[str] = []
    required_non_empty = [
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "spending_changes_needed",
        "decision_explanation",
    ]
    for r in output_rows:
        rid = r.get("request_id", "")
        for col in required_non_empty:
            val = r.get(col)
            if val is None or val.strip() == "":
                r3_violations.append(rid)
                break
    results.append((3, "No required field empty (except earliest_date_for_full_payment)", r3_violations))

    # Rule 4: 0 <= amount_safe_to_pay <= requested_amount, on every row.
    r4_violations: list[str] = []
    for r in output_rows:
        rid = r.get("request_id", "")
        req = requests_by_id.get(rid)
        if not req:
            r4_violations.append(rid)
            continue
        try:
            val = Decimal(r.get("amount_safe_to_pay", ""))
            req_amt = Decimal(req["requested_amount"])
            if val < Decimal("0") or val > req_amt:
                r4_violations.append(rid)
        except Exception:
            r4_violations.append(rid)
    results.append((4, "0 <= amount_safe_to_pay <= requested_amount on every row", r4_violations))

    # Rule 5: affordability_status is one of affordable_now, affordable_with_plan, affordable_later, not_affordable.
    r5_violations: list[str] = []
    for r in output_rows:
        rid = r.get("request_id", "")
        if r.get("affordability_status") not in ALLOWED_STATUSES:
            r5_violations.append(rid)
    results.append((5, "affordability_status is an allowed value", r5_violations))

    # Rule 6: recommended_payment_method is one of full_payment, partial_payment, installments, wait, not_recommended.
    r6_violations: list[str] = []
    for r in output_rows:
        rid = r.get("request_id", "")
        if r.get("recommended_payment_method") not in ALLOWED_PAYMENT_METHODS:
            r6_violations.append(rid)
    results.append((6, "recommended_payment_method is an allowed value", r6_violations))

    # Rule 7: If affordability_status is affordable_now then earliest_date_for_full_payment equals request_date.
    r7_violations: list[str] = []
    for r in output_rows:
        if r.get("affordability_status") == "affordable_now":
            rid = r.get("request_id", "")
            req = requests_by_id.get(rid)
            if not req or r.get("earliest_date_for_full_payment") != req.get("request_date"):
                r7_violations.append(rid)
    results.append((7, "affordable_now implies earliest_date_for_full_payment == request_date", r7_violations))

    # Rule 8: If recommended_payment_method is full_payment, partial_payment or installments,
    # then that method appears in the user's payment_methods_user_will_consider.
    r8_violations: list[str] = []
    immediate_methods = {"full_payment", "partial_payment", "installments"}
    for r in output_rows:
        method = r.get("recommended_payment_method", "")
        if method in immediate_methods:
            rid = r.get("request_id", "")
            req = requests_by_id.get(rid)
            if not req:
                r8_violations.append(rid)
                continue
            prof = profiles_by_user.get(req.get("user_id", ""))
            if not prof:
                r8_violations.append(rid)
                continue
            user_methods = {
                m.strip()
                for m in (prof.get("payment_methods_user_will_consider") or "").split("|")
                if m.strip()
            }
            if method not in user_methods:
                r8_violations.append(rid)
    results.append((8, "Recommended immediate payment method accepted by user", r8_violations))

    # Rule 9: If recommended_payment_method is wait, then full_payment appears in payment_methods_user_will_consider.
    r9_violations: list[str] = []
    for r in output_rows:
        if r.get("recommended_payment_method") == "wait":
            rid = r.get("request_id", "")
            req = requests_by_id.get(rid)
            if not req:
                r9_violations.append(rid)
                continue
            prof = profiles_by_user.get(req.get("user_id", ""))
            if not prof:
                r9_violations.append(rid)
                continue
            user_methods = {
                m.strip()
                for m in (prof.get("payment_methods_user_will_consider") or "").split("|")
                if m.strip()
            }
            if "full_payment" not in user_methods:
                r9_violations.append(rid)
    results.append((9, "wait method requires full_payment accepted by user", r9_violations))

    # Rule 10: If recommended_payment_method is partial_payment then all of:
    # the request has allows_partial_payment true; affordability_status is affordable_with_plan;
    # 0 < amount_safe_to_pay < requested_amount; payment_plan has exactly two payments;
    # the first is amount_safe_to_pay dated request_date; the two amounts sum to requested_amount;
    # the second payment date is on or before desired_completion_date.
    r10_violations: list[str] = []
    for r in output_rows:
        if r.get("recommended_payment_method") == "partial_payment":
            rid = r.get("request_id", "")
            req = requests_by_id.get(rid)
            if not req:
                r10_violations.append(rid)
                continue
            if req.get("allows_partial_payment", "").strip().lower() != "true":
                r10_violations.append(rid)
                continue
            if r.get("affordability_status") != "affordable_with_plan":
                r10_violations.append(rid)
                continue
            try:
                amt_safe = Decimal(r.get("amount_safe_to_pay", ""))
                req_amt = Decimal(req["requested_amount"])
                if not (Decimal("0") < amt_safe < req_amt):
                    r10_violations.append(rid)
                    continue
            except Exception:
                r10_violations.append(rid)
                continue
            plan_items = (r.get("payment_plan") or "").split("|")
            if len(plan_items) != 2:
                r10_violations.append(rid)
                continue
            p1_parts = plan_items[0].split(":")
            p2_parts = plan_items[1].split(":")
            if len(p1_parts) != 2 or len(p2_parts) != 2:
                r10_violations.append(rid)
                continue
            d1, a1_str = p1_parts
            d2, a2_str = p2_parts
            if d1 != req.get("request_date"):
                r10_violations.append(rid)
                continue
            try:
                a1 = Decimal(a1_str)
                a2 = Decimal(a2_str)
                if a1 != amt_safe:
                    r10_violations.append(rid)
                    continue
                if a1 + a2 != req_amt:
                    r10_violations.append(rid)
                    continue
            except Exception:
                r10_violations.append(rid)
                continue
            deadline = req.get("desired_completion_date")
            if deadline and d2 > deadline:
                r10_violations.append(rid)
                continue
    results.append((10, "partial_payment satisfies full contract requirements", r10_violations))

    # Rule 11: If recommended_payment_method is installments then payment_plan must exactly match
    # one supplied option in request_payment_options.csv for that request_id:
    # same number of payments, same first payment date, and dates spaced by payment_frequency_days.
    r11_violations: list[str] = []
    for r in output_rows:
        if r.get("recommended_payment_method") == "installments":
            rid = r.get("request_id", "")
            plan_str = r.get("payment_plan", "")
            plan_items = plan_str.split("|")
            plan_dates: list[str] = []
            valid_syntax = True
            for item in plan_items:
                parts = item.split(":")
                if len(parts) != 2:
                    valid_syntax = False
                    break
                plan_dates.append(parts[0])
            if not valid_syntax or not plan_dates:
                r11_violations.append(rid)
                continue
            opts = options_by_request.get(rid, [])
            matched = False
            for opt in opts:
                if opt.get("payment_method") != "installments":
                    continue
                try:
                    n = int(opt["number_of_payments"])
                    freq = int(opt["payment_frequency_days"])
                    start = datetime.strptime(opt["first_payment_date"], "%Y-%m-%d").date()
                except Exception:
                    continue
                if len(plan_dates) != n:
                    continue
                opt_dates = [(start + timedelta(days=i * freq)).isoformat() for i in range(n)]
                if plan_dates == opt_dates:
                    matched = True
                    break
            if not matched:
                r11_violations.append(rid)
    results.append((11, "installments match a supplied option schedule exactly", r11_violations))

    # Rule 12: payment_plan is either the literal none or chronological entries of the form
    # YYYY-MM-DD:amount joined by the pipe character, dates non-decreasing, all amounts positive and parseable.
    r12_violations: list[str] = []
    for r in output_rows:
        rid = r.get("request_id", "")
        plan = r.get("payment_plan", "")
        if plan == "none":
            continue
        entries = plan.split("|")
        if not entries:
            r12_violations.append(rid)
            continue
        prev_date = None
        failed = False
        for e in entries:
            parts = e.split(":")
            if len(parts) != 2:
                failed = True
                break
            d_str, a_str = parts
            try:
                cur_date = datetime.strptime(d_str, "%Y-%m-%d").date()
                amt = Decimal(a_str)
                if amt <= Decimal("0"):
                    failed = True
                    break
                if prev_date is not None and cur_date < prev_date:
                    failed = True
                    break
                prev_date = cur_date
            except Exception:
                failed = True
                break
        if failed:
            r12_violations.append(rid)
    results.append((12, "payment_plan format, non-decreasing dates, positive amounts", r12_violations))

    # Rule 13: If recommended_payment_method is not_recommended then payment_plan is none.
    r13_violations: list[str] = []
    for r in output_rows:
        if r.get("recommended_payment_method") == "not_recommended":
            if r.get("payment_plan") != "none":
                r13_violations.append(r.get("request_id", ""))
    results.append((13, "not_recommended implies payment_plan == 'none'", r13_violations))

    # Rule 14: spending_changes_needed is either the literal none or at most three entries joined by
    # the pipe character, each either stop:<event_id> or reduce_to:<event_id>:<amount>.
    # Every event_id must exist in financial_events.csv for that request's user.
    # Every referenced event must be a recurring expense whose flexibility is not fixed.
    # The same event_id must not appear in both a stop and a reduce_to.
    # For reduce_to, new amount must be >= minimum_allowed_amount where present.
    r14_violations: list[str] = []
    for r in output_rows:
        rid = r.get("request_id", "")
        sc = r.get("spending_changes_needed", "")
        if sc == "none":
            continue
        entries = sc.split("|")
        if len(entries) > 3 or len(entries) == 0:
            r14_violations.append(rid)
            continue
        req = requests_by_id.get(rid)
        if not req:
            r14_violations.append(rid)
            continue
        uid = req.get("user_id", "")
        events_for_user = user_events.get(uid, {})
        seen_events: set[str] = set()
        failed = False
        for e in entries:
            parts = e.split(":")
            if parts[0] == "stop" and len(parts) == 2:
                eid = parts[1]
                if eid in seen_events:
                    failed = True
                    break
                seen_events.add(eid)
                ev = events_for_user.get(eid)
                if not ev or ev.get("flexibility") == "fixed":
                    failed = True
                    break
            elif parts[0] == "reduce_to" and len(parts) == 3:
                eid = parts[1]
                if eid in seen_events:
                    failed = True
                    break
                seen_events.add(eid)
                ev = events_for_user.get(eid)
                if not ev or ev.get("flexibility") == "fixed":
                    failed = True
                    break
                try:
                    new_amt = Decimal(parts[2])
                    min_str = (ev.get("minimum_allowed_amount") or "").strip()
                    if min_str:
                        min_amt = Decimal(min_str)
                        if new_amt < min_amt:
                            failed = True
                            break
                except Exception:
                    failed = True
                    break
            else:
                failed = True
                break
        if failed:
            r14_violations.append(rid)
    results.append((14, "spending_changes_needed valid, non-fixed, respects minimums", r14_violations))

    # Rule 15: decision_explanation is non-empty on every row.
    r15_violations: list[str] = []
    for r in output_rows:
        rid = r.get("request_id", "")
        exp = r.get("decision_explanation", "")
        if not exp or not exp.strip():
            r15_violations.append(rid)
    results.append((15, "decision_explanation non-empty on every row", r15_violations))

    total_violations = sum(len(v) for _, _, v in results)
    return total_violations == 0, results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=None,
        help="Path to output.csv (default: output.csv at repo root)",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Path to dataset directory (default: dataset at repo root)",
    )
    args = parser.parse_args()

    root = repo_root()
    output_path = Path(args.output).resolve() if args.output else (root / "output.csv")
    dataset_dir = Path(args.dataset).resolve() if args.dataset else (root / "dataset")

    print(f"Verifying {output_path.name} against specification (15 rules)...")
    passed, results = verify_output(output_path, dataset_dir)

    for rule_num, desc, violations in results:
        status = "PASS" if not violations else "FAIL"
        count_str = f"{len(violations)} violation(s)" if violations else "0 violations"
        print(f"  Rule {rule_num:2d} ({desc}): {count_str} [{status}]")
        if violations:
            sample = violations[:5]
            sample_str = ", ".join(sample)
            if len(violations) > 5:
                sample_str += f", ... ({len(violations) - 5} more)"
            print(f"           Examples: {sample_str}")

    total_violations = sum(len(v) for _, _, v in results)
    if passed:
        print(f"\nResult: PASS (0 violations across all 15 rules)")
        return 0
    else:
        print(f"\nResult: FAIL ({total_violations} total violation(s) across 15 rules)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
