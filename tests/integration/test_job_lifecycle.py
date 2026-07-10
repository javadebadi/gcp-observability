"""
Integration test for the job lifecycle Pipeline example.

Writes known log entries to a real GCP project, waits for them to be
indexed, syncs them into a local in-memory store, runs the Pipeline,
and asserts the exact timeline produced.

Run with:
    GCP_TEST_PROJECT=my-project pytest -m integration -v

Skipped automatically in normal `pytest` runs (no GCP required).
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer
from gcp_observability.analysis import Pipeline, RegexExtractor

# ── Fixtures ───────────────────────────────────────────────────────────────────

GCP_TEST_PROJECT = os.getenv("GCP_TEST_PROJECT")


@pytest.fixture(scope="module")
def project() -> str:
    if not GCP_TEST_PROJECT:
        pytest.skip("GCP_TEST_PROJECT env var not set — skipping integration tests")
    return GCP_TEST_PROJECT


@pytest.fixture(scope="module")
def seeded_log_name(project: str) -> str:
    """
    Write a fixed set of job lifecycle log entries to GCP and return the
    log name used. Scoped to module so seeding happens once per test run.
    """
    import google.cloud.logging as gcl

    # Unique log name per run so parallel runs don't bleed into each other
    run_id = uuid.uuid4().hex[:8]
    log_name = f"job-tracker-inttest-{run_id}"

    gcp_client = gcl.Client(project=project)
    logger = gcp_client.logger(log_name)

    events = [
        "[job_tracker] STARTED job_id=job_001 name=data_export",
        "[job_tracker] STEP job_id=job_001 step=fetch_data duration=5s",
        "[job_tracker] STEP job_id=job_001 step=transform duration=12s",
        "[job_tracker] FINISHED job_id=job_001 status=success total_duration=17s",
        "[job_tracker] STARTED job_id=job_002 name=report_gen",
        "[job_tracker] STEP job_id=job_002 step=query duration=3s",
        "[job_tracker] FINISHED job_id=job_002 status=failed total_duration=3s",
    ]

    for msg in events:
        logger.log_text(msg, severity="INFO")
        time.sleep(0.1)  # ensure distinct timestamps

    print(f"\nSeeded {len(events)} log entries to {log_name!r}. Waiting for indexing…")
    time.sleep(30)  # Cloud Logging indexing typically takes 10–30 s

    return log_name


@pytest.fixture(scope="module")
def synced_store(project: str, seeded_log_name: str) -> SQLiteStore:
    """Sync the seeded logs into an in-memory SQLite store."""
    start = datetime.now(timezone.utc) - timedelta(minutes=10)

    client = Client(project=project)
    store = SQLiteStore(":memory:")
    syncer = Syncer(client, store)

    result = syncer.sync(
        QueryBuilder().log_name(f"projects/{project}/logs/{seeded_log_name}"),
        project=project,
        sync_id="inttest-job-lifecycle",
        start=start,
        order_by="timestamp asc",
    )

    print(f"Sync result: {result}")
    # GCP may inject a diagnostic metadata entry alongside seeded logs —
    # assert at least 7 (our entries), not exactly 7.
    assert result.fetched >= 7, (
        f"Expected at least 7 entries from GCP, got {result.fetched}. "
        "Indexing may still be in progress — try increasing the sleep in seeded_log_name."
    )
    return store


# ── Pipeline under test ────────────────────────────────────────────────────────

_PIPELINE = Pipeline([
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


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestJobLifecyclePipeline:
    @pytest.fixture(autouse=True)
    def _timeline(self, synced_store: SQLiteStore) -> None:
        entries = synced_store.query(limit=100_000, order="asc")
        self.timeline = _PIPELINE.run(entries)

    def test_all_seven_events_extracted(self) -> None:
        assert len(self.timeline) == 7

    def test_timeline_sorted_by_timestamp(self) -> None:
        timestamps = [e["_timestamp"] for e in self.timeline]
        assert timestamps == sorted(timestamps)

    def test_source_tags_correct(self) -> None:
        sources = [e["_source"] for e in self.timeline]
        assert sources == [
            "started", "step", "step", "finished",   # job_001
            "started", "step", "finished",            # job_002
        ]

    def test_job_001_fields(self) -> None:
        started = next(e for e in self.timeline if e.get("job_id") == "job_001" and e["_source"] == "started")
        assert started["name"] == "data_export"

        steps = [e for e in self.timeline if e.get("job_id") == "job_001" and e["_source"] == "step"]
        assert len(steps) == 2
        assert {s["step"] for s in steps} == {"fetch_data", "transform"}
        assert {s["duration"] for s in steps} == {"5", "12"}

        finished = next(e for e in self.timeline if e.get("job_id") == "job_001" and e["_source"] == "finished")
        assert finished["status"] == "success"
        assert finished["total_duration"] == "17"

    def test_job_002_fields(self) -> None:
        started = next(e for e in self.timeline if e.get("job_id") == "job_002" and e["_source"] == "started")
        assert started["name"] == "report_gen"

        finished = next(e for e in self.timeline if e.get("job_id") == "job_002" and e["_source"] == "finished")
        assert finished["status"] == "failed"
        assert finished["total_duration"] == "3"

    def test_metadata_on_every_event(self) -> None:
        for event in self.timeline:
            assert isinstance(event["_timestamp"], datetime)
            assert event["_severity"] == "INFO"
            assert event["_source"] in {"started", "step", "finished"}
            assert "_insert_id" in event
            assert "_log_name" in event

    def test_no_overlap_between_patterns(self) -> None:
        # Each insert_id should appear exactly once — no entry matched two patterns
        insert_ids = [e["_insert_id"] for e in self.timeline]
        assert len(insert_ids) == len(set(insert_ids)), "Overlap detected: an entry matched more than one pattern"
