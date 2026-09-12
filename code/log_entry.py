"""Append AGENTS.md-compliant entries to the repo-root log.txt.

log.txt is a graded, submitted artifact, so this helper does two jobs:

1. Writes entries as UTF-8 with LF line endings, which AGENTS.md section 7
   requires. PowerShell's Add-Content and Out-File emit CRLF and can add a BOM.
2. Redacts private material before anything reaches the file. Literal strings to
   scrub are read from TRANSCRIPT_REDACT in .env, which is gitignored, so no
   private path is ever hardcoded in this public file. Common API key shapes are
   scrubbed unconditionally.

Usage:
    python code/log_entry.py session-start --tool "Claude Code"
    python code/log_entry.py turn --tool "Claude Code" --title "..."
        --prompt-file p.txt --summary-file s.txt --action "edited X"
    python code/log_entry.py verify     # pre-submission leak scan, exit 1 on a hit

Long free text is read from files (or "-" for stdin) to avoid shell quoting issues.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEADLINE = datetime(2026, 9, 13, 18, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

# Key shapes scrubbed unconditionally, regardless of .env configuration.
SECRET_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"sk-ant-[0-9A-Za-z_\-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"sk-[0-9A-Za-z]{32,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_TOKEN]"),
]


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def load_redactions(root: Path) -> list[str]:
    """Read TRANSCRIPT_REDACT from .env. Longest first so nested paths scrub cleanly."""
    env = root / ".env"
    raw = ""
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("TRANSCRIPT_REDACT="):
                raw = stripped.split("=", 1)[1].strip().strip('"').strip("'")
    terms: set[str] = set()
    for term in (t.strip() for t in raw.split(",")):
        if len(term) < 3:
            continue
        terms.add(term)
        terms.add(term.replace("\\", "/"))
        terms.add(term.replace("/", "\\"))
    return sorted(terms, key=len, reverse=True)


def redact(text: str, terms: list[str]) -> str:
    for term in terms:
        text = re.sub(re.escape(term), "[REDACTED]", text, flags=re.IGNORECASE)
    for pattern, placeholder in SECRET_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def time_remaining() -> str:
    left = DEADLINE - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "deadline passed"
    hours, rem = divmod(int(left.total_seconds()), 3600)
    return f"{hours // 24}d {hours % 24}h {rem // 60}m"


def read_text(source: str | None) -> str:
    if not source:
        return ""
    if source == "-":
        return sys.stdin.read().strip()
    return Path(source).read_text(encoding="utf-8").strip()


def append(root: Path, body: str, terms: list[str]) -> Path:
    log = root / "log.txt"
    # newline="\n" is the whole point: never let Python translate to CRLF.
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(redact(body, terms).rstrip("\n") + "\n\n")
    return log


def verify(root: Path, terms: list[str]) -> int:
    """Scan log.txt for anything that must not be submitted. Exit 1 on a hit."""
    log = root / "log.txt"
    if not log.is_file():
        print("verify: FAIL, log.txt does not exist")
        return 1
    text = log.read_text(encoding="utf-8")
    hits: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        for term in terms:
            if term.lower() in line.lower():
                hits.append(f"  line {number}: private term matched")
        for pattern, _ in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(f"  line {number}: looks like a live credential")
    # newline="" keeps line endings untranslated so a stray CR is actually visible.
    with log.open("r", encoding="utf-8", newline="") as handle:
        if "\r" in handle.read():
            hits.append("  file contains CR bytes, section 7 requires LF only")
    if not terms:
        print("verify: WARNING, TRANSCRIPT_REDACT is empty in .env, only key shapes were checked")
    if hits:
        print(f"verify: FAIL, {len(hits)} issue(s) in log.txt")
        print("\n".join(hits[:40]))
        return 1
    print(f"verify: OK, {text.count('## [')} entries, no private terms, no credentials, LF endings")
    return 0


def context_block(root: Path, tool: str, parent: str) -> str:
    return (
        f"tool={tool}\n"
        f"branch={git(root, 'rev-parse', '--abbrev-ref', 'HEAD')}\n"
        f"repo_root={root}\n"
        f"worktree=main\n"
        f"parent_agent={parent}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["session-start", "turn", "verify"])
    parser.add_argument("--tool", default="", help="Exact harness name, e.g. Claude Code")
    parser.add_argument("--parent", default="none")
    parser.add_argument("--title", default="")
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--summary-file", default=None)
    parser.add_argument("--action", action="append", default=[])
    parser.add_argument("--language", default="py")
    args = parser.parse_args()

    root = repo_root()
    terms = load_redactions(root)

    if args.kind == "verify":
        return verify(root, terms)

    if not args.tool.strip() or args.tool.strip().lower() in {"ai", "agent", "<tool>"}:
        parser.error("--tool must be the exact harness name, not a placeholder or generic label")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.kind == "session-start":
        body = (
            f"## [{stamp}] SESSION START\n\n"
            f"tool={args.tool}\n"
            f"Repo Root: {root}\n"
            f"Branch: {git(root, 'rev-parse', '--abbrev-ref', 'HEAD')}\n"
            f"Worktree: main\n"
            f"Parent Agent: {args.parent}\n"
            f"Language: {args.language}\n"
            f"Time Remaining: {time_remaining()}\n"
        )
    else:
        if not args.title:
            parser.error("--title is required for a turn entry")
        actions = "\n".join(f"* {a}" for a in args.action) or "* (none)"
        body = (
            f"## [{stamp}] {args.title[:80]}\n\n"
            f"User Prompt (verbatim, secrets redacted):\n{read_text(args.prompt_file)}\n\n"
            f"Agent Response Summary:\n{read_text(args.summary_file)}\n\n"
            f"Actions:\n{actions}\n\n"
            f"Context:\n{context_block(root, args.tool, args.parent)}"
        )

    print(f"appended {args.kind} -> {append(root, body, terms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
