"""
Correlate logs across services using a shared trace ID.

Use case
--------
A web request generates a trace ID that propagates through every service it
touches. All log entries for that request — frontend, backend, database —
share the same trace ID, letting you reconstruct the full request timeline
from one query.

Where trace IDs come from in GCP:
  - Cloud Run: if the incoming HTTP request has an X-Cloud-Trace-Context header,
    Cloud Run's built-in request log gets a trace field automatically.
  - Cloud Load Balancer: stamps the trace field on all request logs.
  - Manual instrumentation: any service can write a trace field explicitly when
    calling logger.log_text(..., trace="projects/PROJECT/traces/TRACE_ID").
    No Load Balancer required.
  - Cloud Trace SDK: instruments your code and propagates the context.

This script:
  1. Syncs all logs for a given trace ID from Cloud Logging into a local store.
  2. Applies a Pipeline of three non-overlapping extractors — one per service.
  3. Prints a unified timeline showing the request flow across services.

Log format used in this example
--------------------------------
    [frontend] GET /api/orders status=200 duration=45ms
    [backend]  Processing order query user_id=u42 count=5
    [db]       Query executed table=orders rows=5 duration=12ms

Usage
-----
    export GCP_PROJECT=my-gcp-project
    export TRACE_ID=your-trace-id-here
    python examples/trace_correlator.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer
from gcp_observability.analysis import Pipeline, RegexExtractor, insights

# ── Configuration ──────────────────────────────────────────────────────────────

PROJECT  = os.environ["GCP_PROJECT"]
TRACE_ID = os.environ["TRACE_ID"]   # just the hex ID, not the full projects/... path
DB_PATH  = "trace_correlator.db"
SYNC_ID  = f"trace-{TRACE_ID[:8]}"  # unique sync job per trace

# ── Pipeline ───────────────────────────────────────────────────────────────────
# Three non-overlapping patterns — each matches exactly one service's log format.
# [frontend] has "status=", [backend] has "user_id=", [db] has "table=".

PIPELINE = Pipeline([
    (
        "frontend",
        RegexExtractor(
            r"\[frontend\] (?P<method>\w+) (?P<path>\S+)"
            r" status=(?P<status>\d+) duration=(?P<duration>\d+)ms"
        ),
    ),
    (
        "backend",
        RegexExtractor(
            r"\[backend\] Processing (?P<action>\w+ \w+)"
            r" user_id=(?P<user_id>\w+) count=(?P<count>\d+)"
        ),
    ),
    (
        "db",
        RegexExtractor(
            r"\[db\] Query executed table=(?P<table>\w+)"
            r" rows=(?P<rows>\d+) duration=(?P<duration>\d+)ms"
        ),
    ),
])

# ── Sync ───────────────────────────────────────────────────────────────────────


def sync(lookback_hours: float = 1.0) -> SQLiteStore:
    """Fetch all logs for TRACE_ID from the last *lookback_hours* hours."""
    client = Client()
    store  = SQLiteStore(DB_PATH)
    syncer = Syncer(client, store)

    start = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    result = syncer.sync(
        QueryBuilder().trace(TRACE_ID),
        project=PROJECT,
        sync_id=SYNC_ID,
        start=start,
        order_by="timestamp asc",
    )
    print(result)
    return store


# ── Analyze ────────────────────────────────────────────────────────────────────


def analyze(store: SQLiteStore) -> None:
    entries = store.query(limit=10_000, order="asc")

    if not entries:
        print(f"\nNo entries found for trace {TRACE_ID}.")
        return

    timeline = PIPELINE.run(entries)

    if not timeline:
        print("\nEntries found but none matched the pipeline patterns.")
        print(f"  {len(entries)} raw entries in store — check your regex patterns.")
        return

    # ── Timeline view ──────────────────────────────────────────────────────────
    print(f"\nTrace: {TRACE_ID}   ({len(timeline)} events from {len(entries)} log entries)\n")
    print(f"{'timestamp':<22}  {'service':<10}  details")
    print("-" * 72)
    for event in timeline:
        ts      = event["_timestamp"].strftime("%Y-%m-%dT%H:%M:%S")
        service = event["_source"]
        details = {k: v for k, v in event.items() if not k.startswith("_")}
        print(f"{ts:<22}  {service:<10}  {details}")

    # ── Per-service counts ─────────────────────────────────────────────────────
    print()
    counts = insights.count_by(timeline, by="_source")
    for service, count in counts.items():
        print(f"  {service}: {count} event(s)")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    store = sync()
    analyze(store)
