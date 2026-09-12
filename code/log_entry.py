"""Append AGENTS.md-compliant entries to the repo-root log.txt.

AGENTS.md requires the transcript to be UTF-8 with '\n' line endings and to live
beside the top-level AGENTS.md. On Windows, PowerShell's Add-Content/Out-File
write CRLF (and may add a BOM), which violates that contract -- so all writes go
through this helper instead.

Usage:
    python code/log_entry.py session-start --tool "Claude Code"
    python code/log_entry.py turn --tool "Claude Code" --title "..." \
        --prompt-file p.txt --summary-file s.txt --action "edited X" --action "ran Y"

Long free text is read from files (or '-' for stdin) to avoid shell quoting issues.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEADLINE = datetime(2026, 9, 13, 18, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones/renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10,
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


def append(root: Path, body: str) -> Path:
    log = root / "log.txt"
    # newline="\n" is the whole point: never let Python translate to CRLF.
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(body.rstrip("\n") + "\n\n")
    return log


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
    parser.add_argument("kind", choices=["session-start", "turn"])
    parser.add_argument("--tool", required=True, help="Exact harness name, e.g. 'Claude Code'")
    parser.add_argument("--parent", default="none")
    parser.add_argument("--title", default="")
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--summary-file", default=None)
    parser.add_argument("--action", action="append", default=[])
    parser.add_argument("--language", default="py")
    args = parser.parse_args()

    if not args.tool.strip() or args.tool.strip().lower() in {"ai", "agent", "<tool>"}:
        parser.error("--tool must be the exact harness name, not a placeholder or generic label")

    root = repo_root()
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

    print(f"appended {args.kind} -> {append(root, body)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
