"""Tests for SQLiteStore — all run against :memory: so no files are created."""

from datetime import datetime, timezone

import pytest

from gcp_observability.logging.client import LogEntry
from gcp_observability.storage.sqlite import SQLiteStore


def _entry(
    insert_id: str = "abc123",
    severity: str = "ERROR",
    timestamp: str = "2026-07-09T10:00:00Z",
    payload: object = "something went wrong",
    payload_type: str = "text",
    project_log: str = "projects/my-project/logs/app",
) -> LogEntry:
    return LogEntry(
        log_name=project_log,
        severity=severity,
        timestamp=datetime.fromisoformat(timestamp),
        payload=payload,
        payload_type=payload_type,
        resource_type="cloud_run_revision",
        resource_labels={"service_name": "my-api"},
        labels={},
        insert_id=insert_id,
    )


@pytest.fixture
def store() -> SQLiteStore:
    return SQLiteStore(":memory:")


class TestSave:
    def test_saves_entry(self, store: SQLiteStore) -> None:
        saved = store.save([_entry()])
        assert saved == 1
        assert store.count() == 1

    def test_duplicate_insert_id_ignored(self, store: SQLiteStore) -> None:
        store.save([_entry(insert_id="dup")])
        saved = store.save([_entry(insert_id="dup")])
        assert saved == 0
        assert store.count() == 1

    def test_saves_multiple(self, store: SQLiteStore) -> None:
        entries = [_entry(insert_id=f"id{i}") for i in range(5)]
        saved = store.save(entries)
        assert saved == 5
        assert store.count() == 5

    def test_empty_list_returns_zero(self, store: SQLiteStore) -> None:
        assert store.save([]) == 0

    def test_json_payload_roundtrip(self, store: SQLiteStore) -> None:
        payload = {"message": "ValueError: Bad input", "code": 42}
        store.save([_entry(insert_id="j1", payload=payload, payload_type="json")])
        entries = store.query()
        assert entries[0].payload == payload

    def test_text_payload_roundtrip(self, store: SQLiteStore) -> None:
        store.save([_entry(insert_id="t1", payload="plain text", payload_type="text")])
        entries = store.query()
        assert entries[0].payload == "plain text"


class TestQuery:
    def _populate(self, store: SQLiteStore) -> None:
        store.save(
            [
                _entry(
                    "e1",
                    severity="ERROR",
                    timestamp="2026-07-09T10:00:00Z",
                    project_log="projects/proj-a/logs/app",
                ),
                _entry(
                    "e2",
                    severity="WARNING",
                    timestamp="2026-07-09T10:10:00Z",
                    project_log="projects/proj-a/logs/app",
                ),
                _entry(
                    "e3",
                    severity="INFO",
                    timestamp="2026-07-09T10:20:00Z",
                    project_log="projects/proj-b/logs/app",
                ),
                _entry(
                    "e4",
                    severity="ERROR",
                    timestamp="2026-07-09T10:30:00Z",
                    project_log="projects/proj-b/logs/run",
                ),
            ]
        )

    def test_query_all(self, store: SQLiteStore) -> None:
        self._populate(store)
        assert len(store.query(limit=100)) == 4

    def test_filter_by_project(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(project="proj-a", limit=100)
        assert len(results) == 2
        assert all(e.project == "proj-a" for e in results)

    def test_filter_by_log_id(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(log_id="run", limit=100)
        assert len(results) == 1

    def test_filter_severity_gte(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(severity_gte="ERROR", limit=100)
        assert len(results) == 2
        assert all(e.severity == "ERROR" for e in results)

    def test_filter_severity_gte_warning(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(severity_gte="WARNING", limit=100)
        assert len(results) == 3

    def test_filter_time_range(self, store: SQLiteStore) -> None:
        self._populate(store)
        start = datetime(2026, 7, 9, 10, 5, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 9, 10, 25, 0, tzinfo=timezone.utc)
        results = store.query(start=start, end=end, limit=100)
        assert len(results) == 2

    def test_filter_search(self, store: SQLiteStore) -> None:
        store.save(
            [
                _entry("s1", payload="timeout connecting to db"),
                _entry("s2", payload="ValueError: Bad input"),
            ]
        )
        results = store.query(search="ValueError", limit=100)
        assert len(results) == 1
        assert "ValueError" in results[0].payload

    def test_order_desc(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(order="desc", limit=100)
        timestamps = [e.timestamp for e in results]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_order_asc(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(order="asc", limit=100)
        timestamps = [e.timestamp for e in results]
        assert timestamps == sorted(timestamps)

    def test_limit(self, store: SQLiteStore) -> None:
        self._populate(store)
        results = store.query(limit=2)
        assert len(results) == 2


class TestWatermark:
    def test_no_watermark_returns_none(self, store: SQLiteStore) -> None:
        assert store.get_watermark("job-1") is None

    def test_set_and_get_watermark(self, store: SQLiteStore) -> None:
        ts = datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)
        store.set_watermark("job-1", ts)
        result = store.get_watermark("job-1")
        assert result is not None
        assert result.replace(tzinfo=timezone.utc) == ts.replace(tzinfo=timezone.utc)

    def test_watermark_advances(self, store: SQLiteStore) -> None:
        t1 = datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 9, 11, 0, 0, tzinfo=timezone.utc)
        store.set_watermark("job-1", t1)
        store.set_watermark("job-1", t2)
        result = store.get_watermark("job-1")
        assert result is not None
        assert result.hour == 11

    def test_total_entries_accumulated(self, store: SQLiteStore) -> None:
        ts = datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)
        store.set_watermark("job-1", ts, entries_added=10)
        store.set_watermark("job-1", ts, entries_added=5)
        jobs = store.list_sync_jobs()
        assert jobs[0]["total_entries_synced"] == 15

    def test_list_sync_jobs(self, store: SQLiteStore) -> None:
        ts = datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)
        store.set_watermark("job-a", ts)
        store.set_watermark("job-b", ts)
        jobs = store.list_sync_jobs()
        assert len(jobs) == 2
        assert {j["sync_id"] for j in jobs} == {"job-a", "job-b"}
