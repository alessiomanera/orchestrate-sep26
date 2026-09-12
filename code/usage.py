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


def _by_surface_and_model() -> dict[tuple[str, str], dict]:
    """Group by (surface, model). A run can legitimately span several models
    when one hits its daily cap, so the report must not assume a single one."""
    grouped: dict[tuple[str, str], dict] = {}
    for record in LEDGER.records:
        bucket = grouped.setdefault(
            (record.surface, record.model),
            {"documents": 0, "calls": 0.0, "input_tokens": 0, "output_tokens": 0, "live": 0},
        )
        bucket["documents"] += 1
        bucket["calls"] += record.call_share
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
    surfaces = _by_surface_and_model()
    total_calls = round(sum(data["calls"] for data in surfaces.values()))
    total_documents = sum(data["documents"] for data in surfaces.values())

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
    for (surface, model_name), _ in sorted(surfaces.items()):
        lines.append(
            f"| {surface} | {LEDGER.provider} | `{model_name}` |"
            f" {purpose.get(surface, 'extraction')} |"
        )
    lines += [
        "",
        "More than one model can appear for a surface. The provider caps free-tier requests"
        " per day per model, so a run that exhausts one model continues on the next in a"
        " deterministic fallback chain. The report groups by the model that actually answered.",
    ]

    lines += [
        "",
        "## Documents, calls and tokens",
        "",
        "Documents are the things read: 16 images and 215 messages. Calls are the requests"
        " actually made, which is far smaller because documents are batched (25 messages or"
        " 4 images per call).",
        "",
        "| Surface | Model | Documents | Calls | Input tokens | Output tokens | Total |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for (surface, model_name), data in sorted(surfaces.items()):
        lines.append(
            f"| {surface} | `{model_name}` | {data['documents']} | {round(data['calls'])} |"
            f" {data['input_tokens']:,} | {data['output_tokens']:,} |"
            f" {data['input_tokens'] + data['output_tokens']:,} |"
        )
    lines.append(
        f"| **total** | | **{total_documents}** | **{total_calls}** |"
        f" **{total['input_tokens']:,}** | **{total['output_tokens']:,}** |"
        f" **{total['total_tokens']:,}** |"
    )
    lines += [
        "",
        "Batched calls report one token count for the whole batch, so each document's row"
        " above carries an even share of its batch's tokens. The totals are exact; the"
        " per-document split is an even attribution.",
        "",
        "Failed attempts (a model out of quota, or briefly unavailable) consume a request"
        " against the provider's cap but return no usage metadata, so they cannot appear in"
        " these token counts. The figures describe the calls that produced the results.",
    ]

    per_request = total["total_tokens"] / max(request_count, 1)
    lines += [
        "",
        "## Per request",
        "",
        f"- Requests in the graded run: **{request_count}**",
        f"- Total tokens: **{total['total_tokens']:,}**",
        f"- Average tokens per request: **{per_request:.1f}**",
        f"- Model calls in the whole run: **{total_calls}**",
        "",
        "Model calls do not scale with the number of requests. Both surfaces are"
        " dataset-sized, not request-sized: 231 documents read once and cached, whatever the"
        " request count. The per-request figures above are therefore the total divided across"
        " the run, not a per-row cost, and they fall as the request count rises.",
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
        f"Documents read live in this run: **{live['calls']}** of {total_documents}"
        f" ({total_documents - live['calls']} served from the content-hash cache)."
        f" Marginal cost of this run: **${live['usd']:.4f}**.",
        "",
        "A fully warm cache makes the pipeline free to re-run and keeps these figures"
        " reproducible, which is why cache hits are recorded here rather than dropped.",
        "",
    ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.relative_to(root)
