"""Tests for deterministic quote amount recovery in code/evidence.py.

Verifies that when a document image extraction returns amount null, the amount
is recovered deterministically from the quote ONLY when the quote contains
exactly one unambiguous monetary value matching the parsed currency.
Refuses recovery when the quote is ambiguous, holds multiple amounts, mentions
conflicting currencies, or contains no positive amount.

Usage:
    python code/tests/test_evidence_quote_recovery.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence import _recover_amount_from_quote  # noqa: E402


def test_recovery_unambiguous_image_04_quote():
    """Exact cached quote from image_04 recovers 2854.00 INR."""
    quote = "Item Bill\n\u20b92854.00"
    recovered = _recover_amount_from_quote(quote, "INR")
    assert recovered == 2854.0, f"Expected 2854.0, got {recovered}"


def test_recovery_unambiguous_standard_currencies():
    """Unambiguous quotes with standard currency symbols and codes."""
    assert _recover_amount_from_quote("Total: $125.50", "USD") == 125.50
    assert _recover_amount_from_quote("Net Pay: IDR 4,365,000", "IDR") == 4365000.0
    assert _recover_amount_from_quote("Due: \u20ac450.00", "EUR") == 450.0
    assert _recover_amount_from_quote("Bill: Rs. 1,500.00", "INR") == 1500.0


def test_refusal_multiple_amounts():
    """Quotes containing multiple monetary amounts must be rejected as ambiguous."""
    # Subtotal and total
    q1 = "Subtotal: \u20b92000.00, Total: \u20b92854.00"
    assert _recover_amount_from_quote(q1, "INR") is None, "Did not refuse multiple amounts"

    # Multiple item lines
    q2 = "Item 1: \u20b91000\nItem 2: \u20b91854"
    assert _recover_amount_from_quote(q2, "INR") is None, "Did not refuse multiple item lines"

    # Alternative amounts
    q3 = "\u20b91000 or \u20b92000"
    assert _recover_amount_from_quote(q3, "INR") is None, "Did not refuse alternative amounts"


def test_refusal_conflicting_currencies():
    """Quotes containing conflicting currency symbols or codes must be rejected."""
    # Currency mismatch
    assert _recover_amount_from_quote("Total: $2854.00", "INR") is None

    # Two currencies in one quote
    assert _recover_amount_from_quote("\u20b92854.00 ($35.00)", "INR") is None
    assert _recover_amount_from_quote("Total: \u20ac100 / $110", "EUR") is None


def test_refusal_non_monetary_or_missing():
    """Quotes without matching currency tag or without numbers must be rejected."""
    # Number with no currency tag
    assert _recover_amount_from_quote("Invoice 2854.00", "INR") is None

    # Text only
    assert _recover_amount_from_quote("Unreadable receipt total", "INR") is None

    # Zero or negative amount
    assert _recover_amount_from_quote("\u20b90.00", "INR") is None

    # None or empty inputs
    assert _recover_amount_from_quote(None, "INR") is None
    assert _recover_amount_from_quote("", "INR") is None
    assert _recover_amount_from_quote("Item Bill\n\u20b92854.00", None) is None


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
