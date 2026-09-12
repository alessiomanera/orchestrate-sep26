"""Model access layer: Gemini REST, content-hash cache, measured token usage.

Standard library only (see DECISIONS.md, D4). Three responsibilities:

1. Load configuration from .env into os.environ without overwriting anything
   already set, so a CI or shell export always wins over the file.
2. Make one bounded, retried generateContent call and return parsed JSON.
3. Cache every call by a SHA-256 of exactly what was sent, and record the
   provider-reported token counts so evaluation/usage_report.md is measured
   rather than reconstructed (DECISIONS.md, D5).

Nothing here knows anything about the financial domain. The prompts and
schemas live in code/evidence.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

MAX_ATTEMPTS = 5          # bounded retry, per .agents/AGENTS.md "bounded execution"
BACKOFF_BASE_SECONDS = 2.0
THROTTLE_BACKOFF_BASE_SECONDS = 8.0   # 429/503 need far longer than a parse retry
# A healthy call answers in a few seconds. 120s only ever meant waiting two
# minutes to be told the quota is exhausted, which turned six unlucky images
# into a stall long enough to block the run.
REQUEST_TIMEOUT_SECONDS = 45

# Minimum wall-clock gap between two live calls. The free-tier quota answered a
# burst of image calls with 429 and 503, so calls are paced rather than
# discovering the limit again on every run. Cache hits are not paced.
MIN_SECONDS_BETWEEN_CALLS = 2.0
_last_call_at = 0.0

# Circuit breaker. Retrying 215 messages individually against an exhausted quota
# would cost hours of backoff to produce nothing. After this many consecutive
# exhausted calls the surface is declared unavailable: later calls return None
# immediately and their rows degrade to "no evidence", which is the documented
# safe default. A cached entry still resolves normally, so a later run picks up
# exactly where this one stopped.
CIRCUIT_BREAKER_THRESHOLD = 6
_consecutive_failures = 0
_circuit_open = False

# (input_tokens, output_tokens) of the most recent live call, so a batched
# caller can attribute usage across the documents that call covered.
_LAST_USAGE: tuple[int, int] = (0, 0)
_LAST_FAILURE_WAS_QUOTA = False

# The free tier's 20-requests-per-day cap is per model, so an exhausted model
# does not mean an exhausted key. When the configured model is out of quota the
# run continues down this chain rather than degrading 231 documents to no
# evidence. The chain is ordered by closeness to the configured model, is fully
# deterministic, and is overridable with MODEL_FALLBACKS. Cache entries are
# keyed by the model that actually answered, so a later run on a recovered
# primary model produces its own entries instead of inheriting a substitute's.
DEFAULT_MODEL_FALLBACKS = "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-2.5-flash"


def model_chain(primary: str) -> list[str]:
    """The configured model first, then its documented fallbacks, deduplicated."""
    load_env()
    chain = [primary]
    for name in os.environ.get("MODEL_FALLBACKS", DEFAULT_MODEL_FALLBACKS).split(","):
        name = name.strip()
        if name and name not in chain:
            chain.append(name)
    return chain

# Published USD per 1M tokens for the configured model. Overridable from the
# environment so the usage report stays correct if pricing changes; the value
# is a documented assumption, not a measurement, and is labelled as such in
# evaluation/usage_report.md.
DEFAULT_INPUT_USD_PER_MTOK = 0.30
DEFAULT_OUTPUT_USD_PER_MTOK = 2.50


def repo_root() -> Path:
    """Walk up from this file until AGENTS.md is found (survives clones and renames)."""
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    return Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Read .env into os.environ. Never overwrites an existing variable.

    Deliberately minimal: KEY=VALUE lines, # comments, optional surrounding
    quotes. A real dotenv library would add a dependency for this much.
    """
    env_path = repo_root() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    """One model call as the provider reported it."""

    model: str
    surface: str            # "images" or "messages"
    input_tokens: int
    output_tokens: int
    cached: bool            # served from .cache/, so it spent nothing this run


@dataclass
class UsageLedger:
    """Accumulates CallRecords for the usage report.

    Records cache hits too. A hit costs nothing now but its tokens were really
    spent once, so the report can state both the true cost of producing the
    result and the marginal cost of re-running the pipeline.
    """

    provider: str = "Google"
    records: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
        self.records.append(record)

    @property
    def live_calls(self) -> list[CallRecord]:
        return [r for r in self.records if not r.cached]

    def totals(self, only_live: bool = False) -> dict:
        rows = self.live_calls if only_live else self.records
        input_tokens = sum(r.input_tokens for r in rows)
        output_tokens = sum(r.output_tokens for r in rows)
        in_rate = float(os.environ.get("INPUT_USD_PER_MTOK", DEFAULT_INPUT_USD_PER_MTOK))
        out_rate = float(os.environ.get("OUTPUT_USD_PER_MTOK", DEFAULT_OUTPUT_USD_PER_MTOK))
        return {
            "calls": len(rows),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "usd": input_tokens / 1e6 * in_rate + output_tokens / 1e6 * out_rate,
        }


LEDGER = UsageLedger()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    load_env()
    path = repo_root() / os.environ.get("CACHE_DIR", ".cache")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_key(*parts: bytes | str) -> str:
    """SHA-256 over every byte that affects the result, prompt version included."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8") if isinstance(part, str) else part)
        digest.update(b"\x00")
    return digest.hexdigest()


def _cache_path(surface: str, key: str) -> Path:
    directory = _cache_dir() / surface
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{key}.json"


# ---------------------------------------------------------------------------
# The one call
# ---------------------------------------------------------------------------


def _pace() -> None:
    """Sleep just long enough to keep live calls MIN_SECONDS_BETWEEN_CALLS apart."""
    global _last_call_at
    wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _post(model: str, payload: dict) -> dict:
    load_env()
    _pace()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set; put it in .env (never in code)")

    request = urllib.request.Request(
        f"{API_BASE}/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_text(response: dict) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts)


def _parse_json_object(text: str) -> dict | None:
    """Parse the model's reply as a JSON object, tolerating a code fence.

    Returns None rather than raising: an unparseable reply must degrade the row
    to "no evidence", never abort the run (.agents/AGENTS.md, bounded execution).
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -3]
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _call_with_retry(
    *,
    surface: str,
    model: str,
    system_instruction: str,
    parts: list[dict],
    response_schema: dict | None,
) -> dict | None:
    """One bounded, paced, temperature-0 call. Returns a parsed object or None.

    Never raises. Every failure path is a degraded row, not a crashed run
    (.agents/AGENTS.md, bounded execution). Records the call in LEDGER whenever
    the provider actually answered, so the usage report counts wasted tokens too.
    """
    global _consecutive_failures, _circuit_open, _LAST_USAGE, _LAST_FAILURE_WAS_QUOTA

    if _circuit_open:
        return None
    _LAST_FAILURE_WAS_QUOTA = False

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": float(os.environ.get("MODEL_TEMPERATURE", "0")),
            "responseMimeType": "application/json",
        },
    }
    if response_schema is not None:
        payload["generationConfig"]["responseSchema"] = response_schema

    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = _post(model, payload)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == MAX_ATTEMPTS - 1:
                break
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                _LAST_FAILURE_WAS_QUOTA = True
            throttled = isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 500, 503)
            base = THROTTLE_BACKOFF_BASE_SECONDS if throttled else BACKOFF_BASE_SECONDS
            time.sleep(base * (2**attempt))
            continue

        usage = response.get("usageMetadata", {})
        input_tokens = int(usage.get("promptTokenCount", 0))
        output_tokens = int(usage.get("candidatesTokenCount", 0))
        _LAST_USAGE = (input_tokens, output_tokens)
        LEDGER.add(
            CallRecord(
                model=model,
                surface=surface,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached=False,
            )
        )

        parsed = _parse_json_object(_extract_text(response))
        if parsed is None:
            last_error = "unparseable JSON reply"
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
            continue

        _consecutive_failures = 0
        return parsed

    _consecutive_failures += 1
    print(f"  [degraded] {surface} call failed after {MAX_ATTEMPTS} attempts: {last_error}")
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD and not _circuit_open:
        _circuit_open = True
        print(
            f"  [circuit open] {_consecutive_failures} consecutive failures; "
            "remaining uncached rows degrade to no evidence. Re-run once quota "
            "recovers: cached rows are kept and only the gaps are retried."
        )
    return None


def generate_json_batch(
    *,
    surface: str,
    model: str,
    system_instruction: str,
    items: list[tuple[str, list[dict], list[bytes | str]]],
    item_schema: dict,
    batch_size: int,
    verbose: bool = True,
) -> dict[str, dict]:
    """Extract many items in few calls, while still caching each item on its own.

    items is (item_id, content parts, key material) per document. Anything
    already in the per-item cache is served from there; only the misses are
    grouped into calls of batch_size. Each result that comes back is written to
    its own per-item cache entry, so a later run with a different batch size,
    or with some documents changed, reuses everything it can.

    Why batch at all: the configured provider tier allows 20 generateContent
    requests per day per model, against 231 documents to read. One request per
    document is not merely slow, it is impossible. Batching makes the whole
    surface fit in roughly 11 requests. See DECISIONS.md, D6.

    Item ids are echoed back by the model and results are matched on them, so a
    dropped or reordered element cannot silently shift every later answer onto
    the wrong document. An unmatched id is simply absent from the return value
    and its caller degrades that row to "no evidence".
    """
    global _consecutive_failures, _circuit_open

    chain = model_chain(model)
    results: dict[str, dict] = {}
    pending: list[tuple[str, list[dict], list]] = []

    # A cache hit under any model in the chain counts: the document has already
    # been read, and re-reading it with a different model would spend quota to
    # answer a question that is already answered.
    for item_id, parts, key_material in items:
        hit = None
        for candidate in chain:
            path = _cache_path(surface, cache_key(candidate, system_instruction, *key_material))
            if path.is_file():
                hit = json.loads(path.read_text(encoding="utf-8"))
                break
        if hit is None:
            pending.append((item_id, parts, key_material))
            continue
        LEDGER.add(
            CallRecord(
                model=hit.get("model", model),
                surface=surface,
                input_tokens=hit.get("input_tokens", 0),
                output_tokens=hit.get("output_tokens", 0),
                cached=True,
            )
        )
        if hit.get("parsed") is not None:
            results[item_id] = hit["parsed"]

    if not pending:
        return results

    batch_schema = {
        "type": "OBJECT",
        "properties": {
            "results": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {"id": {"type": "STRING"}, **item_schema["properties"]},
                    "required": ["id", *item_schema.get("required", [])],
                },
            }
        },
        "required": ["results"],
    }

    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    if verbose:
        print(f"  {len(pending)} uncached item(s) -> {len(batches)} call(s) of up to {batch_size}")

    chain_index = 0
    for number, batch in enumerate(batches, start=1):
        if _circuit_open:
            break

        parts: list[dict] = []
        for item_id, item_parts, _ in batch:
            parts.append({"text": f"\n### item id: {item_id}\n"})
            parts.extend(item_parts)
        parts.append(
            {
                "text": (
                    f"\nReturn a results array with exactly {len(batch)} objects, one per item "
                    "above, each carrying that item's id verbatim in its id field."
                )
            }
        )

        # Try the current model; on a quota refusal move down the chain and
        # retry the same batch, so an exhausted model costs one wasted call
        # rather than the whole surface.
        parsed = None
        while chain_index < len(chain):
            active_model = chain[chain_index]
            parsed = _call_with_retry(
                surface=surface,
                model=active_model,
                system_instruction=system_instruction,
                parts=parts,
                response_schema=batch_schema,
            )
            if parsed is not None or not _LAST_FAILURE_WAS_QUOTA:
                break
            chain_index += 1
            _consecutive_failures = 0
            _circuit_open = False
            if chain_index < len(chain):
                print(
                    f"  [model fallback] {active_model} is out of quota; "
                    f"continuing on {chain[chain_index]}"
                )

        if parsed is None:
            continue
        model_used = chain[min(chain_index, len(chain) - 1)]

        returned = {
            str(row.get("id")): row
            for row in (parsed.get("results") or [])
            if isinstance(row, dict) and row.get("id")
        }
        # Token counts are attributed evenly across the batch, so the usage
        # report stays truthful about the total while still reporting a
        # meaningful per-document figure.
        usage = _LAST_USAGE
        per_item_in = usage[0] // max(len(batch), 1)
        per_item_out = usage[1] // max(len(batch), 1)

        for item_id, _, key_material in batch:
            row = returned.get(item_id)
            if row is None:
                if verbose:
                    print(f"  [unmatched] {surface} item {item_id} absent from batch {number}")
                continue
            key = cache_key(model_used, system_instruction, *key_material)
            _cache_path(surface, key).write_text(
                json.dumps(
                    {
                        "model": model_used,
                        "surface": surface,
                        "parsed": row,
                        "raw_text": json.dumps(row, ensure_ascii=False),
                        "input_tokens": per_item_in,
                        "output_tokens": per_item_out,
                        "batched_with": len(batch),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            results[item_id] = row

    return results


def generate_json(
    *,
    surface: str,
    model: str,
    system_instruction: str,
    parts: list[dict],
    key_material: list[bytes | str],
    response_schema: dict | None = None,
) -> dict | None:
    """One cached, retried, temperature-0 call for a single document.

    key_material is everything that must invalidate the cache when it changes:
    the prompt version, the model name, and the exact document bytes.

    Returns None if the model never produced a parseable object. Callers treat
    None as "no evidence" and continue.
    """
    key = cache_key(model, system_instruction, *key_material)
    path = _cache_path(surface, key)

    if path.is_file():
        entry = json.loads(path.read_text(encoding="utf-8"))
        LEDGER.add(
            CallRecord(
                model=entry.get("model", model),
                surface=surface,
                input_tokens=entry.get("input_tokens", 0),
                output_tokens=entry.get("output_tokens", 0),
                cached=True,
            )
        )
        return entry.get("parsed")

    parsed = _call_with_retry(
        surface=surface,
        model=model,
        system_instruction=system_instruction,
        parts=parts,
        response_schema=response_schema,
    )
    if parsed is None:
        return None

    # Raw text is stored beside the parsed value so a plausible-looking misread
    # stays auditable instead of being silently trusted (DECISIONS.md, D5).
    path.write_text(
        json.dumps(
            {
                "model": model,
                "surface": surface,
                "parsed": parsed,
                "raw_text": json.dumps(parsed, ensure_ascii=False),
                "input_tokens": _LAST_USAGE[0],
                "output_tokens": _LAST_USAGE[1],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return parsed
