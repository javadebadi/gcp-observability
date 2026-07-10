"""
Extractors — Stage 1 of the analysis pipeline.

An extractor is any callable ``LogEntry -> dict | None``.
- Return a ``dict`` of extracted fields to keep the entry.
- Return ``None`` to skip the entry (no match, wrong payload type, etc.).

Every returned dict always includes four metadata fields (prefixed with ``_``)
so downstream analysis can always reference timestamp, severity, and origin:

    _timestamp  datetime  UTC-aware timestamp of the log entry
    _severity   str       e.g. "INFO", "ERROR"
    _insert_id  str       unique Cloud Logging entry ID
    _log_name   str       full log name, e.g. "projects/x/logs/app"

Built-in extractors
-------------------
RegexExtractor
    Captures named or positional groups from the entry's text payload (or a
    specific field inside a JSON payload).

JsonExtractor
    Picks named dot-path fields out of a ``jsonPayload`` entry.

Top-level helper
----------------
extract(entries, extractor)
    Applies any extractor to a list of entries and returns only the hits.

Example::

    from gcp_observability.analysis import RegexExtractor, JsonExtractor, extract
    from gcp_observability import SQLiteStore

    # --- text payload with named groups ---
    promo = RegexExtractor(
        r"player (?P<player_id>\\d+) reached level (?P<level>\\d+)"
    )
    records = promo.extract(store.query(search="reached level"))
    # records[0] == {"_timestamp": ..., "player_id": "42", "level": "7"}

    # --- json payload field picks ---
    api = JsonExtractor({"user": "context.userId", "status": "response.code"})
    records = api.extract(store.query(log_id="api-requests"))
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional

from ..logging.client import LogEntry


# ── Public helper ──────────────────────────────────────────────────────────────


def extract(
    entries: list[LogEntry],
    extractor: Callable[[LogEntry], Optional[dict]],
) -> list[dict]:
    """
    Apply *extractor* to *entries* and return the non-``None`` results.

    Args:
        entries:   Log entries, typically from ``SQLiteStore.query()`` or
                   ``Client.fetch()``.
        extractor: Any callable matching ``LogEntry -> dict | None``.
                   Use ``RegexExtractor``, ``JsonExtractor``, or your own.

    Returns:
        List of extracted dicts, one per matching entry (skips ``None``).
    """
    results: list[dict] = []
    for entry in entries:
        record = extractor(entry)
        if record is not None:
            results.append(record)
    return results


# ── RegexExtractor ─────────────────────────────────────────────────────────────


class RegexExtractor:
    """
    Extract fields from a log entry's payload using a regular expression.

    **Named groups** (recommended — no extra argument needed)::

        RegexExtractor(r"player (?P<player_id>\\d+) level (?P<level>\\d+)")

    **Positional groups** (pass ``fields`` to name them)::

        RegexExtractor(r"player (\\d+) level (\\d+)", fields=["player_id", "level"])

    By default the extractor searches the text payload. For JSON payloads,
    pass ``json_field`` to target a specific sub-field by dot-path::

        RegexExtractor(r"error (?P<code>\\d+)", json_field="details.message")

    Args:
        pattern:    Regex pattern (searched, not full-matched).
        fields:     Names for positional capture groups. Ignored when the
                    pattern uses named groups.
        json_field: Dot-path into ``jsonPayload`` to use as the search text
                    instead of the top-level text payload.
        flags:      ``re`` flags passed to ``re.compile`` (e.g. ``re.IGNORECASE``).
    """

    def __init__(
        self,
        pattern: str,
        *,
        fields: Optional[list[str]] = None,
        json_field: Optional[str] = None,
        flags: int = 0,
    ) -> None:
        self._re = re.compile(pattern, flags)
        self._fields = fields
        self._json_field = json_field

    def __call__(self, entry: LogEntry) -> Optional[dict]:
        text = self._resolve_text(entry)
        if text is None:
            return None

        m = self._re.search(text)
        if m is None:
            return None

        extracted: dict = m.groupdict() if not self._fields else dict(zip(self._fields, m.groups()))
        return {**_metadata(entry), **extracted}

    def extract(self, entries: list[LogEntry]) -> list[dict]:
        """Convenience: ``extract(entries, self)``."""
        return extract(entries, self)

    def _resolve_text(self, entry: LogEntry) -> Optional[str]:
        if self._json_field is not None:
            return _dig(entry.payload, self._json_field)
        if isinstance(entry.payload, str):
            return entry.payload
        if isinstance(entry.payload, dict):
            # JSON payloads often carry the human-readable message in a "message" key
            msg = entry.payload.get("message") or entry.payload.get("msg")
            return str(msg) if msg is not None else str(entry.payload)
        return None


# ── JsonExtractor ──────────────────────────────────────────────────────────────


class JsonExtractor:
    """
    Pick specific fields out of a ``jsonPayload`` log entry.

    ``fields`` is a mapping of **output key** → **dot-path into jsonPayload**::

        JsonExtractor({
            "user_id":  "context.userId",
            "action":   "event.type",
            "status":   "response.statusCode",
        })

    Entries without a JSON payload are always skipped.
    Entries missing some fields are kept (missing fields are omitted from the
    record) unless ``require_all=True``.

    Args:
        fields:      ``{output_key: json_dot_path}`` mapping.
        require_all: Skip entries that are missing any requested field.
    """

    def __init__(
        self,
        fields: dict[str, str],
        *,
        require_all: bool = False,
    ) -> None:
        self._fields = fields
        self._require_all = require_all

    def __call__(self, entry: LogEntry) -> Optional[dict]:
        if not isinstance(entry.payload, dict):
            return None

        extracted: dict = {}
        for out_key, path in self._fields.items():
            value = _dig(entry.payload, path)
            if value is None:
                if self._require_all:
                    return None
            else:
                extracted[out_key] = value

        return {**_metadata(entry), **extracted} if extracted else None

    def extract(self, entries: list[LogEntry]) -> list[dict]:
        """Convenience: ``extract(entries, self)``."""
        return extract(entries, self)


# ── Internal helpers ───────────────────────────────────────────────────────────


def _metadata(entry: LogEntry) -> dict:
    return {
        "_timestamp": entry.timestamp,
        "_severity": entry.severity,
        "_insert_id": entry.insert_id,
        "_log_name": entry.log_name,
    }


def _dig(payload: object, path: str) -> Optional[str]:
    """Walk a dot-path into a nested dict, return the value as str or None."""
    value: object = payload
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return str(value) if value is not None else None


# ── Pipeline ───────────────────────────────────────────────────────────────────


class Pipeline:
    """
    Run multiple named extractors against the same entries and merge the
    results into a single timeline sorted by ``_timestamp``.

    Each extractor is given a name that is stamped as ``_source`` on every
    record it produces, so you can tell which pattern matched which entry when
    reading the merged timeline.

    Patterns should be designed not to overlap — if an entry matches more than
    one extractor it will appear multiple times in the timeline, which is a
    useful signal that the patterns need tightening.

    Args:
        extractors: List of ``(name, extractor)`` pairs. The name becomes the
                    ``_source`` value on each matched record.

    Example::

        pipeline = Pipeline([
            ("job_started",   RegexExtractor(r"Running this job (?P<job_name>\\w+)")),
            ("config_loaded", RegexExtractor(r"Running this with config (?P<config>\\w+)")),
            ("job_failed",    RegexExtractor(r"Running this failed: (?P<reason>.+)")),
        ])

        timeline = pipeline.run(entries)
        for event in timeline:
            print(event["_source"], event["_timestamp"], event)
    """

    def __init__(
        self,
        extractors: list[tuple[str, Callable[[LogEntry], Optional[dict]]]],
    ) -> None:
        self._extractors = extractors

    def run(self, entries: list[LogEntry]) -> list[dict]:
        """
        Apply every extractor to *entries*, tag each record with ``_source``,
        and return all results merged into a single ``_timestamp``-sorted list.
        """
        all_records: list[dict] = []
        for source, extractor in self._extractors:
            for record in extract(entries, extractor):
                all_records.append({"_source": source, **record})
        return sorted(all_records, key=lambda r: r["_timestamp"])


# ── merge() ────────────────────────────────────────────────────────────────────


def merge(*record_lists: list[dict]) -> list[dict]:
    """
    Merge any number of extracted record lists into one timeline sorted by
    ``_timestamp``.

    Use this when you've already run extractors separately and just need to
    combine the results. For running extractors + merging in one step, use
    ``Pipeline`` instead.

    Example::

        a = extractor_a.extract(entries)
        b = extractor_b.extract(entries)
        timeline = merge(a, b)
    """
    combined = [rec for lst in record_lists for rec in lst]
    return sorted(combined, key=lambda r: r["_timestamp"])


# Preserve type alias for downstream type hints
ExtractedRecord = dict[str, object]
Timestamp = datetime
