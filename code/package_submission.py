"""Build code.zip from git-tracked files only, after running the submission gates.

The safety property is structural rather than careful: the archive is built from
`git ls-files`, so anything gitignored cannot end up inside it. .env, _PERSONAL/,
.agents/, .venv/ and log.txt are excluded because git does not track them, not
because this script remembers to skip them.

Usage:
    python code/package_submission.py            # run gates, then build code.zip
    python code/package_submission.py --check    # run gates only, build nothing
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import zipfile
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

# Tracked but not worth shipping: the organizers already have their own dataset.
EXCLUDE_PREFIXES = ("dataset/",)


def repo_root() -> Path:
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def gate(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{': ' + detail if detail else ''}")
    return ok


def run_gates(root: Path, files: list[str]) -> bool:
    print("Submission gates")
    results = []

    # 1. Nothing private is tracked. This is the one that matters most.
    private = [f for f in files if f == ".env" or f.startswith(("_PERSONAL/", ".agents/", ".venv/"))
               or f == "log.txt"]
    results.append(gate("no private file is tracked by git", not private, ", ".join(private)))

    # 2. The template that documents configuration must ship.
    results.append(gate(".env.example is tracked", ".env.example" in files))

    # 3. Transcript is clean. Delegates to the logger's own verifier.
    verify = subprocess.run(
        [sys.executable, str(root / "code" / "log_entry.py"), "verify"],
        capture_output=True, text=True, cwd=str(root),
    )
    results.append(gate("transcript passes leak scan", verify.returncode == 0,
                        verify.stdout.strip().splitlines()[0] if verify.stdout.strip() else ""))

    # 4. Required deliverable inside the zip. Presence is not enough: AGENTS.md
    #    section 6.5 asks for a report of the final full-dataset run, so the gate
    #    checks that every figure it must carry is actually there and that none
    #    of them is a placeholder or a zero left by a run that never happened.
    results.append(gate(*usage_report_gate(root / "code" / "evaluation" / "usage_report.md")))

    # 4b. code.zip is built from `git ls-files`, so an untracked source file is
    #     silently left out of the submission rather than rejected. Found by the
    #     first real dress rehearsal, which shipped an archive missing a test
    #     file written minutes earlier. Anything ignored is intentional and does
    #     not count; anything else under code/ has to be committed to ship.
    untracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "code/"],
        capture_output=True, text=True,
    )
    omitted = [line for line in untracked.stdout.splitlines() if line.strip()]
    results.append(gate("no untracked source file would be left out of code.zip",
                        not omitted, ", ".join(omitted)))

    # 5. Output contract. Skipped rather than failed while output.csv is still blank.
    out_csv = root / "output.csv"
    if not out_csv.is_file():
        print("  [SKIP] output.csv does not exist yet, contract not checked")
    else:
        with out_csv.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        header = rows[0] if rows else []
        body = [r for r in rows[1:] if r]
        results.append(gate("output.csv column order exact", header == REQUIRED_COLUMNS,
                            "" if header == REQUIRED_COLUMNS else str(header)))

        requests = root / "dataset" / "requests.csv"
        with requests.open(newline="", encoding="utf-8") as handle:
            expected = [r[0] for r in list(csv.reader(handle))[1:] if r]
        results.append(gate(f"output.csv has one row per request ({len(expected)})",
                            len(body) == len(expected), f"found {len(body)}"))
        results.append(gate("output.csv row order matches dataset/requests.csv",
                            [r[0] for r in body] == expected))

    return all(results)


def build(root: Path, files: list[str]) -> Path:
    target = root / "code.zip"
    shipped = [f for f in files if not f.startswith(EXCLUDE_PREFIXES)]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in shipped:
            archive.write(root / name, name)
    size_kb = target.stat().st_size / 1024
    print(f"\nBuilt {target.name}: {len(shipped)} files, {size_kb:.0f} KB")
    print(f"Excluded {len(files) - len(shipped)} tracked dataset files, the organizers supply those.")
    return target


USAGE_REPORT_REQUIRED = {
    "provider and model table": "| Surface | Provider | Model | Purpose |",
    "token table": "| Surface | Model | Documents | Calls | Input tokens |",
    "per-request section": "Average tokens per request",
    "cost section": "Estimated total cost",
    "per-request cost": "Estimated cost per request",
}
PLACEHOLDER_MARKERS = ("TODO", "TBD", "FIXME", "XXX", "<fill", "placeholder", "lorem ipsum")


def usage_report_gate(path: Path) -> tuple[str, bool, str]:
    """(label, passed, detail) for evaluation/usage_report.md.

    An empty or stubbed report passed the old presence check and would have been
    submitted, which is the failure this closes. A report is only accepted if it
    names a provider and model, carries the token table, the per-request averages
    and the cost figures section 6.5 asks for, shows a non-zero token total, and
    contains no placeholder text.
    """
    label = "evaluation/usage_report.md is a real report"
    if not path.is_file():
        return label, False, "file is missing"
    text = path.read_text(encoding="utf-8")
    if len(text.strip()) < 400:
        return label, False, f"only {len(text.strip())} characters, looks like a stub"

    missing = [name for name, marker in USAGE_REPORT_REQUIRED.items() if marker not in text]
    if missing:
        return label, False, "missing " + ", ".join(missing)

    lowered = text.lower()
    found = [marker for marker in PLACEHOLDER_MARKERS if marker.lower() in lowered]
    if found:
        return label, False, "contains placeholder text: " + ", ".join(found)

    # A run that made no calls at all would still render every heading above, so
    # the totals row has to show real tokens.
    totals = [line for line in text.splitlines() if line.startswith("| **total**")]
    if not totals:
        return label, False, "no totals row in the token table"
    digits = "".join(c for c in totals[0].replace(",", "") if c.isdigit())
    if not digits or int(digits) == 0:
        return label, False, "totals row reports zero tokens"

    if "`gemini" not in lowered and "`gpt" not in lowered and "`claude" not in lowered:
        return label, False, "no model is named in backticks"

    return label, True, f"{len(text.splitlines())} lines, totals row present"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run gates only")
    args = parser.parse_args()

    root = repo_root()
    files = tracked_files(root)

    if not run_gates(root, files):
        print("\nGates failed. Nothing was built. Fix the failures above and re-run.")
        return 1

    print("\nAll gates passed.")
    if args.check:
        return 0
    build(root, files)
    print("\nSubmit: code.zip, output.csv, and log.txt as the chat transcript.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
