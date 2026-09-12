"""Writes evaluation/usage_report.md from measured calls, never from estimates.

AGENTS.md section 6.5 requires the submitted code.zip to contain a usage report
covering the final full-dataset run: providers and models, call counts, input
and output tokens, totals and averages per request, and estimated cost.

Every number here except the price per token comes from the provider's own
usageMetadata, recorded at the moment of each call by code/llm.py and carried
through the on-disk cache so a re-run reports the same figures without
re-spending them. The price per token is the one documented assumption and is
labelled as such in the report itself.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from llm import (
    DEFAULT_INPUT_USD_PER_MTOK,
    DEFAULT_OUTPUT_USD_PER_MTOK,
    LEDGER,
    load_env,
    repo_root,
)


def _by_surface() -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    for record in LEDGER.records:
        bucket = grouped.setdefault(
            record.surface,
            {"model": record.model, "calls": 0, "input_tokens": 0, "output_tokens": 0, "live": 0},
        )
        bucket["calls"] += 1
        bucket["input_tokens"] += record.input_tokens
        bucket["output_tokens"] += record.output_tokens
        bucket["live"] += 0 if record.cached else 1
    return grouped


def write_usage_report(request_count: int = 250) -> Path:
    load_env()
    root = repo_root()
    path = root / "code" / "evaluation" / "usage_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    in_rate = float(os.environ.get("INPUT_USD_PER_MTOK", DEFAULT_INPUT_USD_PER_MTOK))
    out_rate = float(os.environ.get("OUTPUT_USD_PER_MTOK", DEFAULT_OUTPUT_USD_PER_MTOK))
    total = LEDGER.totals()
    live = LEDGER.totals(only_live=True)
    surfaces = _by_surface()

    lines = [
        "# Usage report",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the"
        " provider's own `usageMetadata`, recorded per call at the moment of the call.",
        "",
        "## Provider and models",
        "",
        "| Surface | Provider | Model | Purpose |",
        "|---|---|---|---|",
    ]
    purpose = {
        "images": "read the total from each linked document image",
        "messages": "classify each message into a typed financial fact",
    }
    for surface, data in sorted(surfaces.items()):
        lines.append(
            f"| {surface} | {LEDGER.provider} | `{data['model']}` |"
            f" {purpose.get(surface, 'extraction')} |"
        )

    lines += [
        "",
        "## Calls and tokens",
        "",
        "| Surface | Calls | Input tokens | Output tokens | Total |",
        "|---|---:|---:|---:|---:|",
    ]
    for surface, data in sorted(surfaces.items()):
        lines.append(
            f"| {surface} | {data['calls']} | {data['input_tokens']:,} |"
            f" {data['output_tokens']:,} | {data['input_tokens'] + data['output_tokens']:,} |"
        )
    lines.append(
        f"| **total** | **{total['calls']}** | **{total['input_tokens']:,}** |"
        f" **{total['output_tokens']:,}** | **{total['total_tokens']:,}** |"
    )

    per_request = total["total_tokens"] / max(request_count, 1)
    lines += [
        "",
        "## Per request",
        "",
        f"- Requests in the graded run: **{request_count}**",
        f"- Total tokens: **{total['total_tokens']:,}**",
        f"- Average tokens per request: **{per_request:.1f}**",
        f"- Average model calls per request: **{total['calls'] / max(request_count, 1):.2f}**",
        "",
        "Model calls do not scale with the number of requests. Both surfaces are"
        " dataset-sized, not request-sized: 16 images and 215 messages, one call each,"
        " whatever the request count. The per-request figures above are therefore the"
        " total divided across the run, not a per-row cost.",
        "",
        "## Estimated cost",
        "",
        f"Rates applied: ${in_rate:.2f} per 1M input tokens, ${out_rate:.2f} per 1M output"
        " tokens. These two rates are the only assumed values in this report; every token"
        " count above is measured.",
        "",
        f"- Estimated total cost: **${total['usd']:.4f}**",
        f"- Estimated cost per request: **${total['usd'] / max(request_count, 1):.6f}**",
        "",
        "## Cost of a re-run",
        "",
        f"Live calls in this run: **{live['calls']}** of {total['calls']}"
        f" ({total['calls'] - live['calls']} served from the content-hash cache)."
        f" Marginal cost of this run: **${live['usd']:.4f}**.",
        "",
        "A fully warm cache makes the pipeline free to re-run and keeps these figures"
        " reproducible, which is why cache hits are recorded here rather than dropped.",
        "",
    ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.relative_to(root)
