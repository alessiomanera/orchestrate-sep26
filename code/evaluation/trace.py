"""Per-request explain tool: why this row got the answer it got.

Usage:
    python code/evaluation/trace.py <request_id>
    python code/evaluation/trace.py request_07
    python code/evaluation/trace.py request_142 --no-evidence

Works for any id in dataset/requests.csv or dataset/sample_requests.csv. For
the 25 solved sample rows it also prints the organizers' answer beside ours and
names the columns that disagree.

Design rule for this file, and the reason it is trustworthy: it never
recomputes a decision. Every number it prints is either read straight off a
dataset row, or returned by the same function the graded run calls:

    financial_state.reconstruct_state      the position and the forecast
    financial_state._balance_timeline      the projected balance curve
    financial_state.amount_safe_to_pay     the safe amount
    financial_state.earliest_date_for_full_payment
    financial_state._convert               the exchange rate actually applied
    planning.accepted_methods              what the user will consider
    planning.max_installments              the installment cap
    planning._installment_payments         the schedule an option implies
    planning.plan_is_safe                  whether that schedule breaches the floor
    planning.eligible_plans / rank_plans   the shortlist and its order
    planning.classify                      status, method and chosen plan
    main.predict                           the eight emitted values

Where something genuinely cannot be sourced from the real code path, this tool
says so in the output rather than reconstructing it. Two such places exist and
are labelled inline:

  - the cadence of a detected recurring series. The detector does not return
    its median gap, so the gap is measured back off the dates it emitted.
  - the reason a pending or scheduled ledger row was left out. Whether a row is
    IN is authoritative (its event_id either reached the forecast or did not);
    the reason it is OUT is re-derived from the documented filter and is marked
    "reconstructed".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence as ev
import financial_state as fs
import main as mn
import planning as pl

# This tool prints dataset text verbatim, and a Windows console is often cp1252
# while the dataset is UTF-8. Never let a character the console cannot encode
# abort a trace half way through.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WIDTH = 78
SOLVED_COLUMNS = [
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]


# ---------------------------------------------------------------------------
# Presentation helpers. These format, they never decide.
# ---------------------------------------------------------------------------


def rule(title: str = "") -> None:
    if title:
        print("\n" + "=" * WIDTH)
        print(title)
        print("=" * WIDTH)
    else:
        print("-" * WIDTH)


def field(label: str, value, indent: int = 2) -> None:
    print(f"{' ' * indent}{label + ':':<32} {value}")


def money(value: float, currency: str = "") -> str:
    return f"{value:,.2f} {currency}".strip()


def wrap(text: str, indent: int = 6, width: int = WIDTH) -> str:
    words = (text or "").split()
    if not words:
        return f"{' ' * indent}(empty)"
    lines, line = [], ""
    for word in words:
        if len(line) + len(word) + 1 > width - indent:
            lines.append(f"{' ' * indent}{line}")
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(f"{' ' * indent}{line}")
    return "\n".join(lines)


def blank(value: str) -> str:
    return value if (value or "").strip() else "(blank)"


def one_line(text: str) -> str:
    """Collapse a quote onto one line. Extractions sometimes cite a fragment
    that spans a line break in the document, which would break the layout."""
    return " ".join((text or "").split())


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def find_request(root: Path, request_id: str) -> tuple[dict, str, bool]:
    """(row, source filename, is_solved_sample). Searches both request files."""
    for name in ("requests.csv", "sample_requests.csv"):
        path = root / "dataset" / name
        if not path.is_file():
            continue
        for row in mn.read_requests(path):
            if row["request_id"] == request_id:
                return row, name, name == "sample_requests.csv"
    raise SystemExit(
        f"request_id {request_id!r} is in neither dataset/requests.csv nor "
        f"dataset/sample_requests.csv"
    )


def quotes_by_document_id(root: Path) -> dict[str, str]:
    """message_id or image_id -> the verbatim fragment the extraction cited.

    The quote is written to the extraction cache for auditing and deliberately
    never reaches MessageFact, so it is read back from the cache here. This is
    the artifact the real run produced, not a re-extraction.
    """
    found: dict[str, str] = {}
    for path in sorted((root / ".cache").rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        parsed = payload.get("parsed") or {}
        key = parsed.get("id") or parsed.get("image_id")
        if key and parsed.get("quote"):
            found[key] = parsed["quote"]
    return found


def raw_rows(root: Path, name: str) -> list[dict]:
    with (root / "dataset" / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def show_request(request: dict, source: str, currency: str) -> None:
    rule(f"REQUEST {request['request_id']}  (from dataset/{source})")
    field("user", request["user_id"])
    field("request date", request["request_date"])
    field("request type", blank(request.get("request_type", "")))
    field("requested amount", money(float(request["requested_amount"]), currency))
    field("currency", f"{currency} (the user's home currency, from the profile)")
    field("desired completion date", blank(request.get("desired_completion_date", "")))
    field("allows partial payment", blank(request.get("allows_partial_payment", "")))
    print("\n  request text:")
    print(wrap(request.get("request_text", "")))


def show_profile(profile: dict, currency: str) -> None:
    rule("PROFILE")
    field("home currency", profile["home_currency"])
    field("current available balance", money(float(profile["current_available_balance"]), currency))
    field("minimum balance to keep", money(float(profile["minimum_balance_to_keep"]), currency))
    field("financial priorities", blank(profile.get("financial_priorities", "")))
    field("protected categories", blank(profile.get("expense_categories_to_protect", "")))
    field("willing to reduce", blank(profile.get("expense_categories_user_is_willing_to_reduce", "")))
    field("willing to stop", blank(profile.get("expense_categories_user_is_willing_to_stop", "")))
    field("accepted payment methods", blank(profile.get("payment_methods_user_will_consider", "")))
    cap = pl.max_installments(profile)
    raw_cap = blank(profile.get("max_installment_months", ""))
    note = " (blank means this user will not consider installments at all)" if cap <= 0 else ""
    field("max installment months", f"{raw_cap}{note}")


def show_position(
    root: Path,
    state: fs.FinancialState,
    request: dict,
    evidence: ev.Evidence,
    currency: str,
    quotes: dict[str, str],
) -> None:
    rule("RECONSTRUCTED POSITION")
    field("opening balance", money(state.current_available_balance, currency))
    field("as of", state.as_of_date.isoformat())
    field("forecast horizon", f"{fs.FORECAST_HORIZON_DAYS} days")
    if state.incomplete_event_ids:
        field("unresolved blank amounts", ", ".join(state.incomplete_event_ids))
        print("      (excluded from every calculation, never treated as zero)")

    # --- recurring series, read back off the events the detector emitted ----
    print("\n  Recurring series projected into the forecast")
    recurring = [e for e in state.forecast_events if e.source.startswith("recurring:")]
    if not recurring:
        print("    none detected")
    else:
        by_source: dict[str, list[fs.CashEvent]] = {}
        for event in recurring:
            by_source.setdefault(event.source, []).append(event)
        for source, events in sorted(by_source.items()):
            events.sort(key=lambda e: e.event_date)
            key = source.split(":", 1)[1]
            gaps = [
                (b.event_date - a.event_date).days for a, b in zip(events, events[1:])
            ]
            cadence = (
                f"~{sorted(gaps)[len(gaps) // 2]} day gap"
                if gaps
                else "single projected occurrence, cadence not observable here"
            )
            print(f"    {key}")
            print(
                f"      {events[0].series}, {len(events)} projected, "
                f"{money(abs(events[0].amount), currency)} each, {cadence}"
            )
            print(
                f"      dates: "
                + ", ".join(e.event_date.isoformat() for e in events[:6])
                + (" ..." if len(events) > 6 else "")
            )
        print("      (cadence measured back off the emitted dates: the detector")
        print("       does not return its median gap, see the module docstring)")

    # --- pending and scheduled ledger rows ---------------------------------
    print("\n  Pending and scheduled ledger rows in the horizon window")
    horizon_end = state.as_of_date + fs.timedelta(days=fs.FORECAST_HORIZON_DAYS)
    included_ids = {e.event_id for e in state.forecast_events if e.event_id}
    candidates = [
        row
        for row in fs._load_events_by_user().get(state.user_id, [])
        if row["status"] in fs.CASH_STATUSES_FOR_FORECAST
    ]
    if not candidates:
        print("    none")
    for row in sorted(candidates, key=lambda r: r["settlement_date"]):
        settlement = fs._parse_date(row["settlement_date"])
        in_forecast = row["event_id"] in included_ids
        verdict = "RESERVED" if in_forecast else "ignored"
        print(
            f"    [{verdict:8}] {row['event_id']:<12} {row['status']:<9} "
            f"{row['direction']:<6} {row['amount'] or '(blank)':>14} {row['currency']} "
            f"settles {row['settlement_date']}"
        )
        if not in_forecast:
            print(f"                 reason (reconstructed): {exclusion_reason(row, state, horizon_end, evidence)}")

    # --- currency conversions actually applied ------------------------------
    print("\n  Currency conversions applied")
    conversions = [
        row
        for row in fs._load_events_by_user().get(state.user_id, [])
        if row["currency"] and row["currency"] != state.home_currency
    ]
    if not conversions:
        print(f"    none, every event is already in {state.home_currency}")
    else:
        rates = fs._load_rates()
        for row in sorted(conversions, key=lambda r: r["settlement_date"])[:12]:
            raw = row["amount"].strip()
            if not raw:
                print(f"    {row['event_id']}: blank amount, no conversion attempted")
                continue
            key = (row["settlement_date"], row["currency"], state.home_currency)
            rate = rates.get(key)
            try:
                converted = fs._convert(
                    float(raw), row["currency"], state.home_currency, row["settlement_date"]
                )
            except KeyError as exc:
                print(f"    {row['event_id']}: {exc}")
                continue
            shown = f"{rate}" if rate is not None else "inverse of the published pair"
            print(
                f"    {row['event_id']}: {float(raw):,.2f} {row['currency']} -> "
                f"{converted:,.2f} {state.home_currency}  (rate {shown} on {row['settlement_date']})"
            )
        if len(conversions) > 12:
            print(f"    ... and {len(conversions) - 12} more")

    # --- evidence -----------------------------------------------------------
    show_evidence(root, state, request, evidence, currency, quotes)


def exclusion_reason(row: dict, state: fs.FinancialState, horizon_end: date, evidence: ev.Evidence) -> str:
    """Why a pending/scheduled row is not in the forecast.

    Reconstructed from the documented filter in reconstruct_state, using that
    module's own constants so the vocabulary cannot drift. Inclusion itself is
    read from the real state; only this sentence is re-derived.
    """
    settlement = fs._parse_date(row["settlement_date"])
    if settlement <= state.as_of_date:
        return f"settles {row['settlement_date']}, on or before the request date, so it is already in the opening balance"
    if settlement > horizon_end:
        return f"settles {row['settlement_date']}, beyond the {fs.FORECAST_HORIZON_DAYS} day horizon"
    if row["direction"] not in ("debit", "credit"):
        return f"direction {row['direction']!r} is not a cash movement"
    if row["status"] == "pending" and row["direction"] == "credit":
        return "pending credit, not counted until it settles (AGENTS.md 6.3)"
    if fs._row_amount(row, evidence) is None:
        return "amount is blank and no linked image resolved it, so it is never guessed at zero"
    return "removed or rescheduled by an applied message fact, see the evidence section"


def show_evidence(
    root: Path,
    state: fs.FinancialState,
    request: dict,
    evidence: ev.Evidence,
    currency: str,
    quotes: dict[str, str],
) -> None:
    print("\n  Evidence for this user")
    facts = evidence.for_user(state.user_id)
    messages = {r["message_id"]: r for r in raw_rows(root, "messages.csv")}
    if not facts:
        print("    no message facts for this user")
    for fact in sorted(facts, key=lambda f: f.sent_on):
        print(
            f"    {fact.message_id}  sent {fact.sent_on}  from {fact.source_type}  "
            f"kind={fact.kind} target={fact.target}"
        )
        details = []
        if fact.new_recurring_amount is not None:
            details.append(f"new recurring {fact.new_recurring_amount}")
        if fact.one_time_amount is not None:
            details.append(f"one off {fact.one_time_amount} {fact.one_time_direction}")
        if fact.relative_change is not None:
            details.append(f"relative x{fact.relative_change} (parsed, not applied, D12)")
        if fact.effective_date:
            details.append(f"effective {fact.effective_date}")
        if fact.currency:
            details.append(f"currency {fact.currency}")
        details.append(f"confirmed_cash={fact.money_is_confirmed_cash}")
        if fact.contains_embedded_instruction:
            details.append("EMBEDDED INSTRUCTION FLAGGED (observability only, no control path)")
        print(f"      {', '.join(details)}")
        quote = quotes.get(fact.message_id)
        if quote:
            print(wrap(f"quote: {one_line(quote)}", indent=6))
        else:
            print("      quote: not in the extraction cache for this document")
        raw = messages.get(fact.message_id, {}).get("message_text", "")
        if raw:
            print(wrap(f"source text: {one_line(raw)}", indent=6))

    # Which facts left a visible mark on the final forecast.
    marked = sorted(
        {
            part
            for event in state.forecast_events
            for part in event.source.replace("+", ":").split(":")
            if part.startswith("message_")
        }
    )
    print(f"\n    facts visible in the final forecast sources: {', '.join(marked) or 'none'}")
    print("    (a cancellation removes an event and so leaves no source string;")
    print("     the with/without evidence comparison below is the reliable net effect)")

    # Resolved blank amounts that belong to this user.
    images = [r for r in raw_rows(root, "images.csv") if r["user_id"] == state.user_id]
    if images:
        print("\n  Document images linked to this user")
        for row in images:
            resolved = evidence.amount_by_event_id.get(row["related_event_id"])
            if resolved:
                print(
                    f"    {row['image_id']} -> {row['related_event_id']}: "
                    f"{resolved[0]:,.2f} {resolved[1]}"
                )
            else:
                print(f"    {row['image_id']} -> {row['related_event_id']}: not resolved")
            quote = quotes.get(row["image_id"])
            if quote:
                print(wrap(f"quote: {one_line(quote)}", indent=6))


def timeline_facts(state: fs.FinancialState) -> tuple[date, float]:
    """(trough date, trough balance) from the real balance timeline."""
    timeline = fs._balance_timeline(state)
    trough_date, trough = min(timeline, key=lambda point: point[1])
    return trough_date, trough


def show_forecast(state: fs.FinancialState, currency: str) -> None:
    rule("90 DAY FORECAST")
    timeline = fs._balance_timeline(state)
    trough_date, trough = timeline_facts(state)
    field("points in the timeline", len(timeline))
    field("opening balance", money(state.current_available_balance, currency))
    field("closing balance", money(timeline[-1][1], currency))
    field("trough", money(trough, currency))
    field("trough date", trough_date.isoformat())
    field("minimum balance to keep", money(state.minimum_balance_to_keep, currency))
    field("headroom above minimum", money(trough - state.minimum_balance_to_keep, currency))
    if trough < state.minimum_balance_to_keep:
        print("      the projection already breaches the floor before any payment")


def show_decision(
    request: dict,
    profile: dict,
    state: fs.FinancialState,
    requested: float,
    currency: str,
) -> None:
    rule("DECISION")

    safe = fs.amount_safe_to_pay(state, requested)
    earliest = fs.earliest_date_for_full_payment(state, requested)
    timeline = fs._balance_timeline(state)
    worst_dip = min(balance - state.current_available_balance for _, balance in timeline)

    print("  amount_safe_to_pay, from financial_state.amount_safe_to_pay")
    field("opening balance", money(state.current_available_balance, currency), indent=4)
    field("minus minimum to keep", money(-state.minimum_balance_to_keep, currency), indent=4)
    field("plus worst future dip", money(worst_dip, currency), indent=4)
    field("= headroom today", money(
        state.current_available_balance - state.minimum_balance_to_keep + worst_dip, currency), indent=4)
    field("capped at requested", money(requested, currency), indent=4)
    field("amount_safe_to_pay", money(safe, currency), indent=4)
    coverage = safe / requested if requested > 0 else 1.0
    field("coverage (safe / requested)", f"{coverage:.4f}", indent=4)
    print(f"      bands: full at >= {pl.FULL_COVERAGE}, near miss at >= {pl.NEAR_MISS_COVERAGE}")

    print("\n  earliest_date_for_full_payment, from financial_state")
    field("earliest safe full payment", earliest.isoformat() if earliest else "never inside the horizon", indent=4)

    # --- options -----------------------------------------------------------
    print("\n  Payment options supplied for this request")
    methods = pl.accepted_methods(profile)
    cap = pl.max_installments(profile)
    options = pl._load_options_by_request().get(request["request_id"], [])
    if not options:
        print("    none listed")
    for option in options:
        method = option.get("payment_method", "")
        count = pl._as_int(option.get("number_of_payments", ""), 0)
        print(
            f"    {option.get('payment_option_id', ''):<20} {method:<16} "
            f"{pl._as_float(option.get('payment_amount', '')):>14,.2f} x {count or 1}"
            f"  fee {pl._as_float(option.get('financing_fee', '')):,.2f}"
            f"  total {pl._as_float(option.get('total_payable_amount', '')):,.2f}"
        )
        print(f"      verdict: {option_verdict(option, request, profile, state, methods, cap)}")

    # --- ranking and winner -------------------------------------------------
    plans = pl.eligible_plans(request, profile, state, requested)
    ranked = pl.rank_plans(plans)
    print("\n  Eligible plans, in the ranked order planning.rank_plans applies")
    print("    keys: completes by deadline, then total cost, then earlier start,")
    print("          then fewer payments, then option id")
    if not ranked:
        print("    no eligible plan survived the filter")
    for position, plan in enumerate(ranked, start=1):
        print(
            f"    {position}. {plan.method:<16} cost {plan.total_cost:>14,.2f}  "
            f"starts {plan.starts_on}  {plan.payment_count} payment(s)  "
            f"by deadline={plan.completes_by_deadline}  {plan.option_id}"
        )

    status, method, chosen = pl.classify(request, profile, state, requested)
    print("\n  Winner, from planning.classify")
    field("affordability_status", status, indent=4)
    field("recommended_payment_method", method, indent=4)
    field("chosen plan", pl.format_payment_plan(chosen), indent=4)


def option_verdict(
    option: dict,
    request: dict,
    profile: dict,
    state: fs.FinancialState,
    methods: set,
    cap: int,
) -> str:
    """Eligible or rejected, decided with planning's own predicates."""
    method = option.get("payment_method", "")

    if method == "installments":
        if "installments" not in methods:
            return "rejected: this user does not accept installments"
        count = pl._as_int(option.get("number_of_payments", ""), 0)
        if cap <= 0:
            return "rejected: max_installment_months is blank, the user will not consider installments"
        if count <= 0:
            return "rejected: the option lists no payment count"
        if count > cap:
            return f"rejected: {count} payments exceeds the user's cap of {cap}"
        payments = pl._installment_payments(option, state.as_of_date)
        if not pl.plan_is_safe(state, payments):
            return "rejected: the schedule breaches minimum_balance_to_keep (planning.plan_is_safe)"
        return "ELIGIBLE: accepted by the user, within the cap, and safe at every checkpoint"

    if method == "full_payment":
        if "full_payment" not in methods:
            return "rejected: this user does not accept full payment"
        requested = float(request["requested_amount"])
        safe = fs.amount_safe_to_pay(state, requested)
        coverage = safe / requested if requested > 0 else 1.0
        if coverage >= pl.FULL_COVERAGE:
            return "ELIGIBLE: coverage clears the full band, the whole amount is safe today"
        if coverage >= pl.NEAR_MISS_COVERAGE:
            return f"near miss: coverage {coverage:.4f} sits in the spending change band"
        return f"rejected: coverage {coverage:.4f} is below the near miss band"

    if method == "partial_payment":
        if "partial_payment" not in methods:
            return "rejected: this user does not accept partial payment"
        if (request.get("allows_partial_payment", "").strip().lower() != "true"):
            return "rejected: this request does not allow partial payment"
        requested = float(request["requested_amount"])
        safe = fs.amount_safe_to_pay(state, requested)
        earliest = fs.earliest_date_for_full_payment(state, requested)
        if earliest is None:
            return "rejected: no date inside the horizon carries the remainder"
        if not (0 < safe < requested):
            return "rejected: the split is not a real split (safe amount is 0 or the whole request)"
        deadline = pl._parse_date(request.get("desired_completion_date", ""))
        if deadline is not None and earliest > deadline:
            return f"rejected: the second payment on {earliest} misses the {deadline} deadline"
        return "ELIGIBLE: a real two payment split completing on or before the deadline"

    return f"not modelled as a plan type: {method!r}"


def show_emitted(request: dict, evidence: ev.Evidence) -> dict:
    rule("EIGHT EMITTED VALUES  (main.predict, the graded code path)")
    row = mn.predict(request, evidence)
    for column in mn.REQUIRED_COLUMNS:
        value = row[column]
        if column == "decision_explanation":
            print(f"  {column}:")
            print(wrap(value, indent=6))
        else:
            field(column, blank(str(value)))
    return row


def show_ground_truth(request: dict, ours: dict, currency: str) -> None:
    rule("GROUND TRUTH COMPARISON  (solved sample row)")
    disagree = []
    for column in SOLVED_COLUMNS:
        theirs = (request.get(column) or "").strip()
        mine = (ours.get(column) or "").strip()
        match = theirs == mine
        if not match:
            disagree.append(column)
        marker = "ok  " if match else "DIFF"
        if column == "decision_explanation":
            print(f"  [{marker}] {column}")
            print("      theirs:")
            print(wrap(theirs, indent=8))
            print("      ours:")
            print(wrap(mine, indent=8))
        else:
            print(f"  [{marker}] {column}")
            print(f"      theirs: {blank(theirs)}")
            print(f"      ours:   {blank(mine)}")
    print()
    if disagree:
        print(f"  {len(disagree)} column(s) disagree: {', '.join(disagree)}")
    else:
        print("  every column agrees")


def show_evidence_ablation(request: dict, requested: float, currency: str) -> None:
    """The net effect of the evidence layer, measured by the real code path twice."""
    rule("EVIDENCE ABLATION  (same forecast with the model layer switched off)")
    request_date = datetime.strptime(request["request_date"], "%Y-%m-%d").date()
    bare = fs.reconstruct_state(request["user_id"], request_date, evidence=ev.EMPTY_EVIDENCE)
    bare_trough_date, bare_trough = timeline_facts(bare)
    bare_safe = fs.amount_safe_to_pay(bare, requested)
    field("trough without evidence", f"{money(bare_trough, currency)} on {bare_trough_date}")
    field("amount_safe_to_pay without", money(bare_safe, currency))
    print("      (compare with the figures above; an identical pair means no")
    print("       message or image changed this row)")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain one request end to end.")
    parser.add_argument("request_id", help="for example request_07")
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="trace the ledger-only path, with the image and message layer off",
    )
    args = parser.parse_args()

    root = fs.repo_root()
    request, source, is_solved = find_request(root, args.request_id)

    evidence = ev.EMPTY_EVIDENCE if args.no_evidence else ev.collect_evidence()
    quotes = {} if args.no_evidence else quotes_by_document_id(root)

    profile = fs._load_profiles()[request["user_id"]]
    currency = profile["home_currency"]
    requested = float(request["requested_amount"])
    request_date = datetime.strptime(request["request_date"], "%Y-%m-%d").date()
    state = fs.reconstruct_state(request["user_id"], request_date, evidence=evidence)

    show_request(request, source, currency)
    show_profile(profile, currency)
    show_position(root, state, request, evidence, currency, quotes)
    show_forecast(state, currency)
    show_decision(request, profile, state, requested, currency)
    ours = show_emitted(request, evidence)
    if not args.no_evidence:
        show_evidence_ablation(request, requested, currency)
    if is_solved:
        show_ground_truth(request, ours, currency)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
