"""
Integration test for trace-based log correlation.

Writes log entries with an explicit trace field to GCP (no Load Balancer
required — the Python client lets you set trace on any entry), waits for
indexing, syncs them back by trace ID, and asserts the Pipeline output.

Run with:
    GCP_TEST_PROJECT=my-project pytest -m integration -v

Note on trace IDs
-----------------
GCP stores the full trace path: ``projects/{project}/traces/{id}``.
Cloud Logging's ``trace:`` filter does a substring match, so querying with
just the hex ID works fine.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer
from gcp_observability.analysis import Pipeline, RegexExtractor, insights
import os

GCP_TEST_PROJECT = os.getenv("GCP_TEST_PROJECT")

_PIPELINE = Pipeline([
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
            r"\[backend\] Processing (?P<action>[\w ]+)"
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


@pytest.fixture(scope="module")
def project() -> str:
    if not GCP_TEST_PROJECT:
        pytest.skip("GCP_TEST_PROJECT env var not set")
    return GCP_TEST_PROJECT


@pytest.fixture(scope="module")
def trace_id(project: str) -> str:
    """
    Seed three log entries sharing a unique trace ID and return that ID.
    The trace field is set explicitly — no Load Balancer or Cloud Trace SDK
    needed.
    """
    import google.cloud.logging as gcl

    hex_id   = uuid.uuid4().hex          # e.g. "a1b2c3d4..."
    full_trace = f"projects/{project}/traces/{hex_id}"
    log_name = f"trace-inttest-{hex_id[:8]}"

    gcp_client = gcl.Client(project=project)
    logger     = gcp_client.logger(log_name)

    entries = [
        "[frontend] GET /api/orders status=200 duration=45ms",
        "[backend] Processing order query user_id=u42 count=5",
        "[db] Query executed table=orders rows=5 duration=12ms",
    ]

    for msg in entries:
        logger.log_text(msg, severity="INFO", trace=full_trace)
        time.sleep(0.1)

    print(f"\nSeeded 3 entries with trace {hex_id!r}. Waiting for indexing…")
    time.sleep(30)

    return hex_id


@pytest.fixture(scope="module")
def synced_store(project: str, trace_id: str) -> SQLiteStore:
    """Sync all entries for the seeded trace into an in-memory store."""
    start = datetime.now(timezone.utc) - timedelta(minutes=10)

    client = Client(project=project)
    store  = SQLiteStore(":memory:")
    syncer = Syncer(client, store)

    result = syncer.sync(
        QueryBuilder().trace(trace_id),
        project=project,
        sync_id=f"inttest-trace-{trace_id[:8]}",
        start=start,
        order_by="timestamp asc",
    )

    print(f"Sync result: {result}")
    assert result.fetched >= 3, (
        f"Expected at least 3 entries, got {result.fetched}. "
        "Indexing may still be in progress."
    )
    return store


@pytest.mark.integration
class TestTraceCorrelator:
    @pytest.fixture(autouse=True)
    def _timeline(self, synced_store: SQLiteStore) -> None:
        entries = synced_store.query(limit=1000, order="asc")
        self.timeline = _PIPELINE.run(entries)

    def test_three_events_extracted(self) -> None:
        assert len(self.timeline) == 3

    def test_one_event_per_service(self) -> None:
        sources = {e["_source"] for e in self.timeline}
        assert sources == {"frontend", "backend", "db"}

    def test_timeline_sorted_by_timestamp(self) -> None:
        timestamps = [e["_timestamp"] for e in self.timeline]
        assert timestamps == sorted(timestamps)

    def test_frontend_fields(self) -> None:
        event = next(e for e in self.timeline if e["_source"] == "frontend")
        assert event["method"] == "GET"
        assert event["path"] == "/api/orders"
        assert event["status"] == "200"
        assert event["duration"] == "45"

    def test_backend_fields(self) -> None:
        event = next(e for e in self.timeline if e["_source"] == "backend")
        assert event["user_id"] == "u42"
        assert event["count"] == "5"

    def test_db_fields(self) -> None:
        event = next(e for e in self.timeline if e["_source"] == "db")
        assert event["table"] == "orders"
        assert event["rows"] == "5"
        assert event["duration"] == "12"

    def test_no_overlap(self) -> None:
        insert_ids = [e["_insert_id"] for e in self.timeline]
        assert len(insert_ids) == len(set(insert_ids))

    def test_count_by_service(self) -> None:
        counts = insights.count_by(self.timeline, by="_source")
        assert counts == {"frontend": 1, "backend": 1, "db": 1}

    def test_filter_by_service(self) -> None:
        db_events = insights.filter_by(self.timeline, _source="db")
        assert len(db_events) == 1
        assert db_events[0]["table"] == "orders"

    def test_all_entries_share_trace(self, trace_id: str, synced_store: SQLiteStore) -> None:
        # Every raw entry in the store should reference our trace ID
        entries = synced_store.query(limit=1000)
        assert all(
            e.trace is not None and trace_id in e.trace
            for e in entries
            if e.trace  # skip the GCP diagnostic entry which has no trace
        )
