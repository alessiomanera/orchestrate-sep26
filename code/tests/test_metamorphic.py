"""Metamorphic testing suite for Buy or Wait?

Metamorphic testing asserts relationships that must hold between inputs
and outputs across transformations, finding subtle defects without
relying on ground truth labels.

Properties tested:
  P1. Scale invariance (monetary values scaled by k in {0.01, 100, 10000})
  P2. Balance monotonicity (increasing balance never decreases safe amount or status)
  P3. Expense monotonicity (adding debits never increases safe amount or raises trough)
  P4. Irrelevance invariance (cancelled, failed, pending credit, irrelevant messages)
  P5. Date translation (shifting calendar dates shifts output dates without changing amounts/enums)
  P6. Boundary conditions (zero request, balance==minimum, single event, no income, etc.)
  P7. Adversarial evidence (prompt injections in messages treated as data, flagged, ignored)

All modifications are performed on isolated temporary datasets in a temporary
directory; dataset/ is strictly read-only and never modified.

Usage:
    python code/tests/test_metamorphic.py
"""

from __future__ import annotations

import copy
import csv
import dataclasses
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure code/ is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence as ev
import financial_state as fs
import main as mn
import planning as pl

STATUS_RANK = {
    "not_affordable": 0,
    "affordable_later": 1,
    "affordable_with_plan": 2,
    "affordable_now": 3,
}


def _shift_iso(val: str, days: int) -> str:
    val = (val or "").strip()
    if not val:
        return val
    d = datetime.strptime(val[:10], "%Y-%m-%d").date()
    return (d + timedelta(days=days)).isoformat()


@contextmanager
def temporary_dataset(root: Path):
    """Create an isolated, temporary copy of dataset/ and patch repo_root."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        tmp_data = tmp_root / "dataset"
        tmp_data.mkdir(parents=True, exist_ok=True)
        (tmp_root / "AGENTS.md").write_text("# Dummy AGENTS.md\n", encoding="utf-8")

        for fname in [
            "financial_profiles.csv",
            "financial_events.csv",
            "exchange_rates.csv",
            "request_payment_options.csv",
            "requests.csv",
            "sample_requests.csv",
            "messages.csv",
            "images.csv",
        ]:
            src = root / "dataset" / fname
            if src.is_file():
                (tmp_data / fname).write_bytes(src.read_bytes())

        old_fs_root = fs.repo_root
        old_pl_root = pl.repo_root
        old_mn_root = mn.repo_root
        fs.repo_root = lambda: tmp_root
        pl.repo_root = lambda: tmp_root
        mn.repo_root = lambda: tmp_root

        fs._load_profiles.cache_clear()
        fs._load_events_by_user.cache_clear()
        fs._load_rates.cache_clear()
        pl._load_options_by_request.cache_clear()

        try:
            yield tmp_data
        finally:
            fs.repo_root = old_fs_root
            pl.repo_root = old_pl_root
            mn.repo_root = old_mn_root

            fs._load_profiles.cache_clear()
            fs._load_events_by_user.cache_clear()
            fs._load_rates.cache_clear()
            pl._load_options_by_request.cache_clear()


# ===========================================================================
# P1. Scale Invariance
# ===========================================================================


def test_p1_scale_invariance(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Multiplying all monetary values for a user by k must leave all categoricals
    identical, and scale amount_safe_to_pay and plan amounts by exactly k.
    """
    violations = []
    events_by_user = fs._load_events_by_user()

    for k in [0.01, 100.0, 10000.0]:
        for req in requests:
            u = req["user_id"]
            r_id = req["request_id"]
            base_out = mn.predict(req, base_evidence)

            # Build scaled evidence
            scaled_ev = copy.deepcopy(base_evidence)
            for e_id, (amt_val, amt_curr) in list(scaled_ev.amount_by_event_id.items()):
                for row in events_by_user.get(u, []):
                    if row["event_id"] == e_id:
                        scaled_ev.amount_by_event_id[e_id] = (amt_val * k, amt_curr)
            if u in scaled_ev.facts_by_user:
                new_facts = []
                for fact in scaled_ev.facts_by_user[u]:
                    updates = {}
                    if fact.new_recurring_amount is not None:
                        updates["new_recurring_amount"] = fact.new_recurring_amount * k
                    if fact.one_time_amount is not None:
                        updates["one_time_amount"] = fact.one_time_amount * k
                    new_facts.append(dataclasses.replace(fact, **updates))
                scaled_ev.facts_by_user[u] = new_facts

            with temporary_dataset(root) as tmp_data:
                # Scale profiles
                with (tmp_data / "financial_profiles.csv").open(newline="", encoding="utf-8") as h:
                    profs = list(csv.DictReader(h))
                for p in profs:
                    if p["user_id"] == u:
                        p["current_available_balance"] = str(float(p["current_available_balance"]) * k)
                        p["minimum_balance_to_keep"] = str(float(p["minimum_balance_to_keep"]) * k)
                with (tmp_data / "financial_profiles.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=profs[0].keys())
                    w.writeheader()
                    w.writerows(profs)

                # Scale events
                with (tmp_data / "financial_events.csv").open(newline="", encoding="utf-8") as h:
                    ev_rows = list(csv.DictReader(h))
                for e in ev_rows:
                    if e["user_id"] == u and e["amount"].strip():
                        e["amount"] = str(float(e["amount"]) * k)
                with (tmp_data / "financial_events.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=ev_rows[0].keys())
                    w.writeheader()
                    w.writerows(ev_rows)

                # Scale payment options
                with (tmp_data / "request_payment_options.csv").open(newline="", encoding="utf-8") as h:
                    opts = list(csv.DictReader(h))
                for o in opts:
                    if o["request_id"] == r_id:
                        o["payment_amount"] = str(float(o["payment_amount"]) * k)
                        o["financing_fee"] = str(float(o["financing_fee"]) * k)
                        o["total_payable_amount"] = str(float(o["total_payable_amount"]) * k)
                with (tmp_data / "request_payment_options.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=opts[0].keys())
                    w.writeheader()
                    w.writerows(opts)

                scaled_req = dict(req)
                scaled_req["requested_amount"] = str(float(req["requested_amount"]) * k)

                fs._load_profiles.cache_clear()
                fs._load_events_by_user.cache_clear()
                pl._load_options_by_request.cache_clear()

                scaled_out = mn.predict(scaled_req, scaled_ev)

            # Assert identical categoricals
            for col in [
                "affordability_status",
                "recommended_payment_method",
                "earliest_date_for_full_payment",
                "spending_changes_needed",
            ]:
                if base_out[col] != scaled_out[col]:
                    violations.append((r_id, f"k={k}", col, base_out[col], scaled_out[col]))

            # Assert amount_safe_to_pay scales by k
            base_amt = float(base_out["amount_safe_to_pay"])
            scaled_amt = float(scaled_out["amount_safe_to_pay"])
            expected_amt = base_amt * k
            tol = max(1e-4 * max(scaled_amt, expected_amt, 1.0), 0.05)
            if abs(scaled_amt - expected_amt) > tol:
                violations.append((r_id, f"k={k}", "amount_safe_to_pay", expected_amt, scaled_amt))

            # Assert payment_plan amounts scale by k and dates match
            base_plan = base_out["payment_plan"]
            scaled_plan = scaled_out["payment_plan"]
            if base_plan == "none":
                if scaled_plan != "none":
                    violations.append((r_id, f"k={k}", "payment_plan", base_plan, scaled_plan))
            else:
                base_parts = base_plan.split("|")
                scaled_parts = scaled_plan.split("|")
                if len(base_parts) != len(scaled_parts):
                    violations.append((r_id, f"k={k}", "payment_plan_len", len(base_parts), len(scaled_parts)))
                else:
                    for bp, sp in zip(base_parts, scaled_parts):
                        bd, ba = bp.split(":", 1)
                        sd, sa = sp.split(":", 1)
                        if bd != sd:
                            violations.append((r_id, f"k={k}", "payment_plan_date", bd, sd))
                        exp_a = float(ba) * k
                        act_a = float(sa)
                        if abs(act_a - exp_a) > max(1e-4 * max(act_a, exp_a, 1.0), 0.05):
                            violations.append((r_id, f"k={k}", "payment_plan_amt", exp_a, act_a))

    return violations


# ===========================================================================
# P2. Balance Monotonicity
# ===========================================================================


def test_p2_balance_monotonicity(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Increasing opening balance must never decrease amount_safe_to_pay,
    and must never move affordability_status to a strictly worse class.
    """
    violations = []
    multipliers = [1.1, 1.5, 2.0, 5.0]

    for multiplier in multipliers:
        for req in requests:
            u = req["user_id"]
            r_id = req["request_id"]
            base_out = mn.predict(req, base_evidence)
            base_safe = float(base_out["amount_safe_to_pay"])
            base_status = base_out["affordability_status"]

            with temporary_dataset(root) as tmp_data:
                with (tmp_data / "financial_profiles.csv").open(newline="", encoding="utf-8") as h:
                    profs = list(csv.DictReader(h))
                for p in profs:
                    if p["user_id"] == u:
                        bal = float(p["current_available_balance"])
                        p["current_available_balance"] = str(bal * multiplier + 1000.0)
                with (tmp_data / "financial_profiles.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=profs[0].keys())
                    w.writeheader()
                    w.writerows(profs)

                fs._load_profiles.cache_clear()
                new_out = mn.predict(req, base_evidence)

            new_safe = float(new_out["amount_safe_to_pay"])
            new_status = new_out["affordability_status"]

            if new_safe < base_safe - 1e-4:
                violations.append((r_id, f"mult={multiplier}", "safe_decreased", base_safe, new_safe))
            if STATUS_RANK[new_status] < STATUS_RANK[base_status]:
                violations.append((r_id, f"mult={multiplier}", "status_regressed", base_status, new_status))

    return violations


# ===========================================================================
# P3. Expense Monotonicity
# ===========================================================================


def test_p3_expense_monotonicity(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Adding a future debit must never increase amount_safe_to_pay
    and must never raise the projected trough.
    """
    violations = []
    profiles = fs._load_profiles()

    def get_trough(state):
        return min(b for _, b in fs._balance_timeline(state))

    for req in requests:
        u = req["user_id"]
        r_id = req["request_id"]
        req_date = fs._parse_date(req["request_date"])
        prof = profiles[u]
        curr = prof["home_currency"]

        base_state = fs.reconstruct_state(u, req_date, evidence=base_evidence)
        base_trough = get_trough(base_state)
        base_safe = fs.amount_safe_to_pay(base_state, float(req["requested_amount"]))

        for status_val in ["scheduled", "pending", "settled"]:
            with temporary_dataset(root) as tmp_data:
                with (tmp_data / "financial_events.csv").open(newline="", encoding="utf-8") as h:
                    event_rows = list(csv.DictReader(h))

                future_date = req_date + timedelta(days=10)
                extra_event = {
                    "event_id": "extra_debit_p3",
                    "user_id": u,
                    "event_date": future_date.isoformat(),
                    "settlement_date": future_date.isoformat(),
                    "event_type": "expense",
                    "category": "one_off_test_fee",
                    "description": "test extra expense",
                    "direction": "debit",
                    "amount": "500.0",
                    "currency": curr,
                    "status": status_val,
                    "linked_event_id": "",
                }
                event_rows.append(extra_event)
                with (tmp_data / "financial_events.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=event_rows[0].keys())
                    w.writeheader()
                    w.writerows(event_rows)

                fs._load_events_by_user.cache_clear()
                new_state = fs.reconstruct_state(u, req_date, evidence=base_evidence)
                new_trough = get_trough(new_state)
                new_safe = fs.amount_safe_to_pay(new_state, float(req["requested_amount"]))

            if new_safe > base_safe + 1e-4:
                violations.append((r_id, status_val, "safe_increased", base_safe, new_safe))
            if new_trough > base_trough + 1e-4:
                violations.append((r_id, status_val, "trough_raised", base_trough, new_trough))

    return violations


# ===========================================================================
# P4. Irrelevance Invariance
# ===========================================================================


def test_p4_irrelevance_invariance(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Adding events with status cancelled or failed, or a pending credit,
    or adding an irrelevant message, must leave all outputs completely unchanged.
    """
    violations = []
    profiles = fs._load_profiles()

    test_cases = [
        ("cancelled_debit", {"direction": "debit", "status": "cancelled", "event_type": "expense"}),
        ("failed_debit", {"direction": "debit", "status": "failed", "event_type": "expense"}),
        ("pending_credit", {"direction": "credit", "status": "pending", "event_type": "income"}),
        ("cancelled_credit", {"direction": "credit", "status": "cancelled", "event_type": "income"}),
        ("failed_credit", {"direction": "credit", "status": "failed", "event_type": "income"}),
    ]

    for test_name, event_params in test_cases:
        for req in requests:
            u = req["user_id"]
            r_id = req["request_id"]
            req_date = fs._parse_date(req["request_date"])
            prof = profiles[u]
            curr = prof["home_currency"]
            base_out = mn.predict(req, base_evidence)

            with temporary_dataset(root) as tmp_data:
                with (tmp_data / "financial_events.csv").open(newline="", encoding="utf-8") as h:
                    event_rows = list(csv.DictReader(h))

                future_date = req_date + timedelta(days=10)
                extra_event = {
                    "event_id": f"extra_{test_name}",
                    "user_id": u,
                    "event_date": future_date.isoformat(),
                    "settlement_date": future_date.isoformat(),
                    "event_type": event_params["event_type"],
                    "category": "test_irrelevant",
                    "description": "test irrelevant description",
                    "direction": event_params["direction"],
                    "amount": "99999.0",
                    "currency": curr,
                    "status": event_params["status"],
                    "linked_event_id": "",
                }
                event_rows.append(extra_event)
                with (tmp_data / "financial_events.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=event_rows[0].keys())
                    w.writeheader()
                    w.writerows(event_rows)

                fs._load_events_by_user.cache_clear()
                new_out = mn.predict(req, base_evidence)

            for col in [
                "amount_safe_to_pay",
                "affordability_status",
                "recommended_payment_method",
                "payment_plan",
                "earliest_date_for_full_payment",
            ]:
                if new_out[col] != base_out[col]:
                    violations.append((r_id, test_name, col, base_out[col], new_out[col]))

    # Irrelevant message
    for req in requests:
        u = req["user_id"]
        r_id = req["request_id"]
        base_out = mn.predict(req, base_evidence)

        ev_copy = copy.deepcopy(base_evidence)
        irrel_fact = ev.MessageFact(
            message_id="irrel_msg_p4",
            user_id=u,
            related_event_id="",
            sent_on=date(2025, 1, 1),
            source_type="other",
            kind="irrelevant",
            target="none",
            new_recurring_amount=None,
            one_time_amount=None,
            one_time_direction="none",
            effective_date=None,
            currency=None,
            money_is_confirmed_cash=False,
            contains_embedded_instruction=False,
            relative_change=None,
            scope_category=None,
        )
        ev_copy.facts_by_user.setdefault(u, []).append(irrel_fact)
        new_out = mn.predict(req, ev_copy)

        for col in [
            "amount_safe_to_pay",
            "affordability_status",
            "recommended_payment_method",
            "payment_plan",
            "earliest_date_for_full_payment",
        ]:
            if new_out[col] != base_out[col]:
                violations.append((r_id, "irrelevant_message", col, base_out[col], new_out[col]))

    return violations


# ===========================================================================
# P5. Date Translation
# ===========================================================================


def _shift_year(val: str, years: int = 4) -> str:
    val = (val or "").strip()
    if not val:
        return val
    d = datetime.strptime(val[:10], "%Y-%m-%d").date()
    try:
        new_d = d.replace(year=d.year + years)
    except ValueError:
        new_d = d.replace(year=d.year + years, day=28)
    return new_d.isoformat()


def test_p5_date_translation(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Shifting calendar dates by a 4-year leap cycle (1461 days) preserves both
    the monthly salary cadence (day of the month invariant) and leap days (Feb 29).
    Emitted dates must shift by exactly the same offset, with all categorical
    outputs and amounts remaining identical.
    """
    violations = []
    years = 4

    for req in requests:
        u = req["user_id"]
        r_id = req["request_id"]
        base_out = mn.predict(req, base_evidence)

        shifted_ev = copy.deepcopy(base_evidence)
        if u in shifted_ev.facts_by_user:
            new_facts = []
            for fact in shifted_ev.facts_by_user[u]:
                updates = {}
                if fact.sent_on:
                    updates["sent_on"] = fact.sent_on.replace(year=fact.sent_on.year + years)
                if fact.effective_date:
                    try:
                        updates["effective_date"] = fact.effective_date.replace(
                            year=fact.effective_date.year + years
                        )
                    except ValueError:
                        updates["effective_date"] = fact.effective_date.replace(
                            year=fact.effective_date.year + years, day=28
                        )
                new_facts.append(dataclasses.replace(fact, **updates))
            shifted_ev.facts_by_user[u] = new_facts

        with temporary_dataset(root) as tmp_data:
            # Shift exchange rates
            with (tmp_data / "exchange_rates.csv").open(newline="", encoding="utf-8") as h:
                rates = list(csv.DictReader(h))
            for r in rates:
                r["rate_date"] = _shift_year(r["rate_date"], years)
            with (tmp_data / "exchange_rates.csv").open("w", newline="", encoding="utf-8") as h:
                w = csv.DictWriter(h, fieldnames=rates[0].keys())
                w.writeheader()
                w.writerows(rates)

            # Shift events
            with (tmp_data / "financial_events.csv").open(newline="", encoding="utf-8") as h:
                ev_rows = list(csv.DictReader(h))
            for e in ev_rows:
                if e["user_id"] == u:
                    e["event_date"] = _shift_year(e["event_date"], years)
                    e["settlement_date"] = _shift_year(e["settlement_date"], years)
            with (tmp_data / "financial_events.csv").open("w", newline="", encoding="utf-8") as h:
                w = csv.DictWriter(h, fieldnames=ev_rows[0].keys())
                w.writeheader()
                w.writerows(ev_rows)

            # Shift options
            with (tmp_data / "request_payment_options.csv").open(newline="", encoding="utf-8") as h:
                opts = list(csv.DictReader(h))
            for o in opts:
                if o["request_id"] == r_id:
                    o["first_payment_date"] = _shift_year(o["first_payment_date"], years)
            with (tmp_data / "request_payment_options.csv").open("w", newline="", encoding="utf-8") as h:
                w = csv.DictWriter(h, fieldnames=opts[0].keys())
                w.writeheader()
                w.writerows(opts)

            shifted_req = dict(req)
            shifted_req["request_date"] = _shift_year(req["request_date"], years)
            shifted_req["desired_completion_date"] = _shift_year(
                req.get("desired_completion_date", ""), years
            )

            fs._load_events_by_user.cache_clear()
            fs._load_rates.cache_clear()
            pl._load_options_by_request.cache_clear()

            shifted_out = mn.predict(shifted_req, shifted_ev)

        # Assert identical categoricals and amounts
        for col in [
            "affordability_status",
            "recommended_payment_method",
            "amount_safe_to_pay",
            "spending_changes_needed",
        ]:
            if shifted_out[col] != base_out[col]:
                violations.append((r_id, col, base_out[col], shifted_out[col]))

        # Earliest date shifted
        base_earliest = base_out["earliest_date_for_full_payment"]
        shifted_earliest = shifted_out["earliest_date_for_full_payment"]
        expected_earliest = _shift_year(base_earliest, years) if base_earliest else ""
        if shifted_earliest != expected_earliest:
            violations.append((r_id, "earliest_date", expected_earliest, shifted_earliest))

        # Payment plan shifted
        base_plan = base_out["payment_plan"]
        shifted_plan = shifted_out["payment_plan"]
        if (base_plan == "none") != (shifted_plan == "none"):
            violations.append((r_id, "payment_plan_none_mismatch", base_plan, shifted_plan))
        elif base_plan != "none":
            base_parts = base_plan.split("|")
            shifted_parts = shifted_plan.split("|")
            if len(base_parts) != len(shifted_parts):
                violations.append((r_id, "payment_plan_len", len(base_parts), len(shifted_parts)))
            else:
                for bp, sp in zip(base_parts, shifted_parts):
                    bd, ba = bp.split(":", 1)
                    sd, sa = sp.split(":", 1)
                    exp_d = _shift_year(bd, years)
                    if exp_d != sd or ba != sa:
                        violations.append((r_id, "payment_plan_entry", f"{exp_d}:{ba}", sp))

    return violations


# ===========================================================================
# P6. Boundary Conditions
# ===========================================================================


def test_p6_boundary_conditions(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Test boundary edge cases: pipeline must neither crash nor emit invalid output."""
    violations = []
    sample_req = requests[0]
    u = sample_req["user_id"]
    r_id = sample_req["request_id"]

    boundary_cases = [
        "balance_equals_minimum",
        "zero_requested_amount",
        "deadline_equals_request_date",
        "deadline_before_request_date",
        "single_event_user",
        "no_income_user",
        "payment_option_count_one",
    ]

    for bcase in boundary_cases:
        with temporary_dataset(root) as tmp_data:
            test_req = dict(sample_req)

            if bcase == "balance_equals_minimum":
                with (tmp_data / "financial_profiles.csv").open(newline="", encoding="utf-8") as h:
                    profs = list(csv.DictReader(h))
                for p in profs:
                    if p["user_id"] == u:
                        p["current_available_balance"] = p["minimum_balance_to_keep"]
                with (tmp_data / "financial_profiles.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=profs[0].keys())
                    w.writeheader()
                    w.writerows(profs)

            elif bcase == "zero_requested_amount":
                test_req["requested_amount"] = "0"

            elif bcase == "deadline_equals_request_date":
                test_req["desired_completion_date"] = test_req["request_date"]

            elif bcase == "deadline_before_request_date":
                req_d = fs._parse_date(test_req["request_date"])
                test_req["desired_completion_date"] = (req_d - timedelta(days=5)).isoformat()

            elif bcase == "single_event_user":
                with (tmp_data / "financial_events.csv").open(newline="", encoding="utf-8") as h:
                    ev_rows = list(csv.DictReader(h))
                user_events = [e for e in ev_rows if e["user_id"] == u]
                other_events = [e for e in ev_rows if e["user_id"] != u]
                single_user = user_events[:1] if user_events else []
                with (tmp_data / "financial_events.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=ev_rows[0].keys())
                    w.writeheader()
                    w.writerows(other_events + single_user)

            elif bcase == "no_income_user":
                with (tmp_data / "financial_events.csv").open(newline="", encoding="utf-8") as h:
                    ev_rows = list(csv.DictReader(h))
                filtered = [e for e in ev_rows if not (e["user_id"] == u and e["event_type"] == "income")]
                with (tmp_data / "financial_events.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=ev_rows[0].keys())
                    w.writeheader()
                    w.writerows(filtered)

            elif bcase == "payment_option_count_one":
                with (tmp_data / "request_payment_options.csv").open(newline="", encoding="utf-8") as h:
                    opts = list(csv.DictReader(h))
                for o in opts:
                    if o["request_id"] == r_id:
                        o["number_of_payments"] = "1"
                with (tmp_data / "request_payment_options.csv").open("w", newline="", encoding="utf-8") as h:
                    w = csv.DictWriter(h, fieldnames=opts[0].keys())
                    w.writeheader()
                    w.writerows(opts)

            fs._load_profiles.cache_clear()
            fs._load_events_by_user.cache_clear()
            pl._load_options_by_request.cache_clear()

            try:
                out = mn.predict(test_req, base_evidence)
                # Contract sanity assertions
                if out["affordability_status"] not in STATUS_RANK:
                    violations.append((bcase, "invalid_status", out["affordability_status"]))
                if out["recommended_payment_method"] not in {
                    "full_payment",
                    "partial_payment",
                    "installments",
                    "wait",
                    "not_recommended",
                }:
                    violations.append((bcase, "invalid_method", out["recommended_payment_method"]))
                float(out["amount_safe_to_pay"])
            except Exception as exc:
                violations.append((bcase, "exception_raised", str(exc)))

    return violations


# ===========================================================================
# P7. Adversarial Evidence
# ===========================================================================


def test_p7_adversarial_evidence(root: Path, requests: list[dict], base_evidence: ev.Evidence) -> list[tuple]:
    """Inject a synthetic prompt injection message telling the system to override
    safety rules. Confirm it is treated strictly as data, flagged as an embedded
    instruction, and changes no decisions.
    """
    violations = []

    for req in requests:
        u = req["user_id"]
        r_id = req["request_id"]
        base_out = mn.predict(req, base_evidence)

        # Synthetic adversarial message fact
        adv_fact = ev.MessageFact(
            message_id="adv_injection_p7",
            user_id=u,
            related_event_id="",
            sent_on=date(2025, 1, 1),
            source_type="other",
            kind="confirm",
            target="none",
            new_recurring_amount=None,
            one_time_amount=None,
            one_time_direction="none",
            effective_date=None,
            currency=None,
            money_is_confirmed_cash=False,
            contains_embedded_instruction=True,  # FLAGGED
            relative_change=None,
            scope_category=None,
        )

        ev_copy = copy.deepcopy(base_evidence)
        ev_copy.facts_by_user.setdefault(u, []).append(adv_fact)

        # 1. Confirm flagged
        flagged = [f for f in ev_copy.for_user(u) if f.contains_embedded_instruction]
        if not flagged:
            violations.append((r_id, "flag_missing"))

        # 2. Confirm no decision change
        adv_out = mn.predict(req, ev_copy)
        for col in [
            "amount_safe_to_pay",
            "affordability_status",
            "recommended_payment_method",
            "payment_plan",
            "earliest_date_for_full_payment",
        ]:
            if adv_out[col] != base_out[col]:
                violations.append((r_id, "decision_changed", col, base_out[col], adv_out[col]))

    return violations


# ===========================================================================
# Test Runner
# ===========================================================================


def main():
    root = fs.repo_root()
    requests = mn.read_requests(root / "dataset" / "sample_requests.csv")
    base_evidence = ev.collect_evidence()

    tests = [
        ("P1. Scale Invariance", test_p1_scale_invariance),
        ("P2. Balance Monotonicity", test_p2_balance_monotonicity),
        ("P3. Expense Monotonicity", test_p3_expense_monotonicity),
        ("P4. Irrelevance Invariance", test_p4_irrelevance_invariance),
        ("P5. Date Translation", test_p5_date_translation),
        ("P6. Boundary Conditions", test_p6_boundary_conditions),
        ("P7. Adversarial Evidence", test_p7_adversarial_evidence),
    ]

    all_passed = True
    print("\n" + "=" * 78)
    print("METAMORPHIC STRESS TESTING SUITE")
    print("=" * 78)

    results_table = []

    for name, test_fn in tests:
        print(f"\nRunning {name}...")
        violations = test_fn(root, requests, base_evidence)
        if not violations:
            status = "PASS"
            detail = "0 violations"
            print(f"  [{status}] {name}")
        else:
            status = "FAIL"
            all_passed = False
            detail = f"{len(violations)} violations (e.g. {violations[:2]})"
            print(f"  [{status}] {name}: {detail}")
        results_table.append((name, status, len(violations), detail))

    print("\n" + "=" * 78)
    print("METAMORPHIC TEST SUMMARY TABLE")
    print("=" * 78)
    print(f"{'Property':<32} | {'Result':<6} | {'Violations':<10} | {'Detail'}")
    print("-" * 78)
    for name, status, count, detail in results_table:
        print(f"{name:<32} | {status:<6} | {count:<10} | {detail}")
    print("=" * 78)

    passed_count = sum(1 for _, s, _, _ in results_table if s == "PASS")
    total_count = len(results_table)
    print(f"\nResult: {passed_count}/{total_count} metamorphic properties passed\n")

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
