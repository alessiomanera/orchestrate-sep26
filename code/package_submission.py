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

    # 4. Required deliverable inside the zip.
    results.append(gate("evaluation/usage_report.md present",
                        (root / "code" / "evaluation" / "usage_report.md").is_file()))

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
