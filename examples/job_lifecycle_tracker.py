"""
Track the full lifecycle of batch jobs using Pipeline.

Use case
--------
A batch job logs three distinct event types:

    [job_tracker] STARTED job_id=job_abc123 name=data_export
    [job_tracker] STEP job_id=job_abc123 step=fetch_data duration=5s
    [job_tracker] STEP job_id=job_abc123 step=transform duration=12s
    [job_tracker] FINISHED job_id=job_abc123 status=success total_duration=17s

This script:
  1. Syncs all [job_tracker] logs into local SQLite.
  2. Applies a Pipeline of three non-overlapping regex extractors — one per
     event type — to produce a unified timeline.
  3. Prints the timeline so you can read the full sequence of events across
     all jobs in one view.

Usage
-----
    export GCP_PROJECT=my-gcp-project
    python examples/job_lifecycle_tracker.py

Configuration
-------------
Set GCP_PROJECT in the environment. DB_PATH and SYNC_ID can be overridden
as needed.
"""

from __future__ import annotations

import os
from datetime import datetime

from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer
from gcp_observability.analysis import Pipeline, RegexExtractor

# ── Configuration ──────────────────────────────────────────────────────────────

PROJECT = os.environ["GCP_PROJECT"]
DB_PATH = "job_lifecycle.db"
SYNC_ID = "job-lifecycle"
LOG_NAME = "job-tracker"  # gcloud log name used when writing events

# ── Pipeline ───────────────────────────────────────────────────────────────────
# Three non-overlapping patterns — each matches exactly one event type.
# STARTED has "name=", STEP has "step=", FINISHED has "status=".

PIPELINE = Pipeline([
    (
        "started",
        RegexExtractor(
            r"\[job_tracker\] STARTED job_id=(?P<job_id>\w+) name=(?P<name>\w+)"
        ),
    ),
    (
        "step",
        RegexExtractor(
            r"\[job_tracker\] STEP job_id=(?P<job_id>\w+)"
            r" step=(?P<step>\w+) duration=(?P<duration>\d+)s"
        ),
    ),
    (
        "finished",
        RegexExtractor(
            r"\[job_tracker\] FINISHED job_id=(?P<job_id>\w+)"
            r" status=(?P<status>\w+) total_duration=(?P<total_duration>\d+)s"
        ),
    ),
])

# ── Sync ───────────────────────────────────────────────────────────────────────


def sync(start: datetime | None = None) -> SQLiteStore:
    client = Client()
    store = SQLiteStore(DB_PATH)
    syncer = Syncer(client, store)

    watermark = store.get_watermark(SYNC_ID)
    if watermark:
        print(f"Resuming from watermark: {watermark.isoformat()}")
    else:
        print("First run — fetching last 24 h by default")

    result = syncer.sync(
        QueryBuilder()
        .log_name(f"projects/{PROJECT}/logs/{LOG_NAME}"),
        project=PROJECT,
        sync_id=SYNC_ID,
        start=start,
    )
    print(result)
    return store


# ── Analyze ────────────────────────────────────────────────────────────────────


def analyze(store: SQLiteStore) -> list[dict]:
    entries = store.query(limit=100_000, order="asc")

    if not entries:
        print("\nNo job lifecycle entries in local store.")
        return []

    timeline = PIPELINE.run(entries)

    if not timeline:
        print("\nNo events matched the pipeline patterns.")
        return []

    print(f"\n{'timestamp':<32}  {'_source':<10}  details")
    print("-" * 80)
    for event in timeline:
        ts = event["_timestamp"].strftime("%Y-%m-%dT%H:%M:%S")
        src = event["_source"]
        details = {k: v for k, v in event.items() if not k.startswith("_")}
        print(f"{ts:<32}  {src:<10}  {details}")

    return timeline


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    store = sync()
    analyze(store)
