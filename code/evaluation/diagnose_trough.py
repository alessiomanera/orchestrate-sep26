"""Back-solve the organizers' forecast trough and compare it against ours.

Hypothesis under test: `amount_safe_to_pay` is
`min(requested_amount, trough - minimum_balance_to_keep)`, where `trough` is the
lowest projected balance across the forecast horizon. If that holds, then on
every row where the constraint binds (truth < requested_amount) we can invert it:

    their_trough = truth + minimum_balance_to_keep

and compare directly against the trough our own forecast computes. That turns an
opaque amount error into a structured one, and the shape of the residual says
which part of the forecast is wrong.

This is a diagnostic. It changes nothing and is not part of the pipeline.

Usage:
    python code/evaluation/diagnose_trough.py
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import financial_state as fs                                  # noqa: E402
from evidence import collect_evidence                         # noqa: E402


def repo_root() -> Path:
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent.parent


def money(value: float) -> str:
    return f"{value:>15,.0f}"


def main() -> int:
    root = repo_root()
    evidence = collect_evidence()

    with (root / "dataset" / "sample_requests.csv").open(newline="", encoding="utf-8") as handle:
        samples = list(csv.DictReader(handle))
    profiles = fs._load_profiles()

    rows = []
    for sample in samples:
        request_date = datetime.strptime(sample["request_date"], "%Y-%m-%d").date()
        requested = float(sample["requested_amount"])
        truth = float(sample["amount_safe_to_pay"])
        profile = profiles[sample["user_id"]]
        minimum = float(profile["minimum_balance_to_keep"])

        state = fs.reconstruct_state(sample["user_id"], request_date, evidence=evidence)
        timeline = fs._balance_timeline(state)
        our_trough = min(balance for _, balance in timeline)

        # Monthly rate of the projected recurring debits, as a scale for the delta.
        horizon_end = request_date + timedelta(days=fs.FORECAST_HORIZON_DAYS)
        recurring_debits = [
            event
            for event in state.forecast_events
            if event.series == "debit" and event.amount < 0
        ]
        monthly_recurring = sum(-e.amount for e in recurring_debits) / 3 if recurring_debits else 0.0

        rows.append(
            {
                "request_id": sample["request_id"],
                "user_id": sample["user_id"],
                "request_type": sample["request_type"],
                "currency": profile["home_currency"],
                "binds": truth < requested - 0.01,
                "truth": truth,
                "requested": requested,
                "minimum": minimum,
                "their_trough": truth + minimum,
                "our_trough": our_trough,
                "delta": our_trough - (truth + minimum),
                "monthly_recurring": monthly_recurring,
                "n_recurring": len(recurring_debits),
                "n_events": len(state.forecast_events),
                "balance": state.current_available_balance,
                "state": state,
                "horizon_end": horizon_end,
                "request_date": request_date,
            }
        )

    binding = [r for r in rows if r["binds"]]

    print("=" * 108)
    print("TROUGH BACK-SOLVE, rows where the constraint binds (truth < requested_amount)")
    print("=" * 108)
    print(
        f"{'request':<12}{'their_trough':>16}{'our_trough':>16}{'delta':>16}"
        f"{'delta/月recur':>16}  {'cur':<5}{'type':<12}"
    )
    for row in binding:
        share = (
            f"{row['delta'] / row['monthly_recurring']:>15.2f}"
            if row["monthly_recurring"]
            else f"{'n/a':>15}"
        )
        print(
            f"{row['request_id']:<12}{money(row['their_trough'])}{money(row['our_trough'])}"
            f"{money(row['delta'])}{share}  {row['currency']:<5}{row['request_type']:<12}"
        )

    non_binding = [r for r in rows if not r["binds"]]
    print(
        f"\n{len(binding)} of {len(rows)} rows bind. "
        f"{len(non_binding)} do not (truth == requested_amount), so their trough is only "
        "bounded below, not pinned, and they are excluded from the analysis below."
    )

    # ---------------------------------------------------------------- structure
    print("\n" + "=" * 108)
    print("IS THERE STRUCTURE IN THE DELTA?")
    print("=" * 108)

    deltas = [r["delta"] for r in binding]
    signs = Counter("over" if d > 0.01 else "under" if d < -0.01 else "exact" for d in deltas)
    print(f"\nSign: {dict(signs)}")

    normalised = [
        r["delta"] / r["monthly_recurring"] for r in binding if r["monthly_recurring"]
    ]
    if normalised:
        print(
            f"Delta as a multiple of monthly recurring spend: "
            f"median {statistics.median(normalised):+.2f}, "
            f"mean {statistics.fmean(normalised):+.2f}, "
            f"range {min(normalised):+.2f} to {max(normalised):+.2f}"
        )

    ratios = [r["our_trough"] / r["their_trough"] for r in binding if r["their_trough"]]
    if ratios:
        print(
            f"our_trough / their_trough: median {statistics.median(ratios):.3f}, "
            f"range {min(ratios):.3f} to {max(ratios):.3f}"
        )

    # Does the delta match a single event in that row's own forecast?
    print("\nDoes the delta equal a single forecast event for that user?")
    matched = 0
    for row in binding:
        target = abs(row["delta"])
        if target < 1:
            continue
        hits = [
            event
            for event in row["state"].forecast_events
            if abs(abs(event.amount) - target) <= max(1.0, 0.02 * target)
        ]
        if hits:
            matched += 1
            print(
                f"  {row['request_id']:<12} delta {row['delta']:>14,.0f} matches "
                f"{hits[0].source} on {hits[0].event_date}"
            )
    print(f"  {matched} of {len(binding)} deltas match a single event within 2%")

    # Does the delta match a whole number of one recurring series' instalments?
    print("\nDoes the delta equal N instalments of one recurring series?")
    multiples = 0
    for row in binding:
        target = abs(row["delta"])
        if target < 1:
            continue
        found = None
        seen_amounts = {
            round(abs(e.amount), 2) for e in row["state"].forecast_events if e.series == "debit"
        }
        for amount in seen_amounts:
            if amount < 1:
                continue
            n = target / amount
            if abs(n - round(n)) < 0.02 and 1 <= round(n) <= 6:
                found = (round(n), amount)
                break
        if found:
            multiples += 1
            print(f"  {row['request_id']:<12} delta = {found[0]} x {found[1]:,.0f}")
    print(f"  {multiples} of {len(binding)} deltas are a small multiple of one recurring amount")

    # Correlations with categorical and count features.
    print("\nDelta by category (median normalised delta, count):")
    for label, key in (("request_type", "request_type"), ("home currency", "currency")):
        print(f"  {label}:")
        groups: dict[str, list[float]] = {}
        for row in binding:
            if row["monthly_recurring"]:
                groups.setdefault(row[key], []).append(row["delta"] / row["monthly_recurring"])
        for name, values in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"    {name:<16} n={len(values):<3} median {statistics.median(values):+.2f}")

    print("\nCorrelation of normalised delta with count features:")
    for label, key in (
        ("recurring debit events", "n_recurring"),
        ("total forecast events", "n_events"),
    ):
        pairs = [
            (row[key], row["delta"] / row["monthly_recurring"])
            for row in binding
            if row["monthly_recurring"]
        ]
        if len(pairs) > 2 and len({p[0] for p in pairs}) > 1:
            try:
                r = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
                print(f"  {label:<26} r = {r:+.3f}  (n={len(pairs)})")
            except statistics.StatisticsError:
                print(f"  {label:<26} undefined")

    # Does the trough sit at the horizon edge? That would mean the horizon length
    # itself, not the events in it, is setting the answer.
    print("\nWhere does our trough fall in the horizon?")
    positions = []
    for row in binding:
        timeline = fs._balance_timeline(row["state"])
        trough_date = min(timeline, key=lambda pair: pair[1])[0]
        day = (trough_date - row["request_date"]).days
        positions.append(day)
    print(f"  trough day offsets: {sorted(positions)}")
    at_edge = sum(1 for d in positions if d >= fs.FORECAST_HORIZON_DAYS - 5)
    at_start = sum(1 for d in positions if d == 0)
    print(
        f"  {at_start} troughs sit at day 0 (no projected dip at all), "
        f"{at_edge} sit within 5 days of the {fs.FORECAST_HORIZON_DAYS}-day horizon edge"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
