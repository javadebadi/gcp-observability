"""Tests for Syncer — all use a mock Client so no GCP calls are made."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from gcp_observability.logging.client import LogEntry
from gcp_observability.storage.sqlite import SQLiteStore
from gcp_observability.sync import Syncer, SyncResult
from gcp_observability import QueryBuilder, Severity


def _make_entry(insert_id: str, ts: str, severity: str = "ERROR") -> LogEntry:
    return LogEntry(
        log_name="projects/my-project/logs/app",
        severity=severity,
        timestamp=datetime.fromisoformat(ts),
        payload=f"error {insert_id}",
        payload_type="text",
        resource_type="cloud_run_revision",
        resource_labels={},
        labels={},
        insert_id=insert_id,
    )


def _syncer(entries: list[LogEntry]) -> tuple[Syncer, SQLiteStore]:
    """Return a Syncer backed by an in-memory store and a mock Client."""
    client = MagicMock()
    client.iter.return_value = iter(entries)
    store = SQLiteStore(":memory:")
    return Syncer(client, store), store


class TestSync:
    def test_first_sync_uses_default_lookback(self) -> None:
        syncer, store = _syncer([])
        result = syncer.sync(QueryBuilder(), project="p", sync_id="s")
        assert result.fetched == 0
        assert store.get_watermark("s") is not None

    def test_first_sync_stores_entries(self) -> None:
        entries = [_make_entry("e1", "2026-07-09T10:00:00+00:00")]
        syncer, store = _syncer(entries)
        result = syncer.sync(QueryBuilder(), project="p", sync_id="s")
        assert result.fetched == 1
        assert result.stored == 1
        assert store.count() == 1

    def test_second_sync_uses_watermark(self) -> None:
        client = MagicMock()
        client.iter.return_value = iter([])
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        syncer.sync(QueryBuilder(), project="p", sync_id="s")
        watermark_after_first = store.get_watermark("s")

        syncer.sync(QueryBuilder(), project="p", sync_id="s")

        # Second call's filter should start from the first watermark
        _, kwargs = client.iter.call_args
        filter_str = kwargs.get("filter_", client.iter.call_args[0][0] if client.iter.call_args[0] else "")
        assert watermark_after_first.strftime("%Y-%m-%dT%H:%M") in filter_str

    def test_duplicates_not_double_stored(self) -> None:
        entry = _make_entry("dup1", "2026-07-09T10:00:00+00:00")
        client = MagicMock()
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        client.iter.return_value = iter([entry])
        r1 = syncer.sync(QueryBuilder(), project="p", sync_id="s")

        # Reset watermark so second sync fetches same entry
        store.set_watermark("s", datetime(2026, 7, 9, 9, 0, 0, tzinfo=timezone.utc))
        client.iter.return_value = iter([entry])
        r2 = syncer.sync(QueryBuilder(), project="p", sync_id="s")

        assert r1.stored == 1
        assert r2.stored == 0          # duplicate ignored
        assert store.count() == 1      # only one row in store

    def test_future_watermark_skips_sync(self) -> None:
        client = MagicMock()
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        # Plant a watermark 1 hour in the future
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        store.set_watermark("s", future)

        result = syncer.sync(QueryBuilder(), project="p", sync_id="s")

        assert result.fetched == 0
        assert result.stored == 0
        client.iter.assert_not_called()   # Cloud Logging never hit

    def test_sync_result_fields(self) -> None:
        entries = [_make_entry("e1", "2026-07-09T10:00:00+00:00")]
        syncer, store = _syncer(entries)
        result = syncer.sync(QueryBuilder(), project="p", sync_id="s")
        assert result.sync_id == "s"
        assert result.fetched == 1
        assert result.stored == 1
        assert result.duplicates == 0
        assert result.since < result.until


class TestBackfill:
    def test_splits_into_windows(self) -> None:
        client = MagicMock()
        client.iter.return_value = iter([])
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        results = syncer.backfill(
            QueryBuilder(),
            project="p",
            sync_id="bf",
            start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 2, tzinfo=timezone.utc),
            window_hours=6,
        )
        assert len(results) == 4   # 24h / 6h = 4 windows

    def test_future_end_is_capped_at_now(self) -> None:
        client = MagicMock()
        client.iter.return_value = iter([])
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        syncer.backfill(
            QueryBuilder(),
            project="p",
            sync_id="bf",
            start=datetime.now(timezone.utc) - timedelta(hours=1),
            end=far_future,
            window_hours=1,
        )
        watermark = store.get_watermark("bf")
        assert watermark is not None
        # Watermark must not be in the future
        assert watermark <= datetime.now(timezone.utc) + timedelta(seconds=5)

    def test_future_watermark_does_not_skip_real_logs(self) -> None:
        """After a future-end backfill, sync() should not miss real logs."""
        client = MagicMock()
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        # Backfill with a future end — watermark should be capped at now
        client.iter.return_value = iter([])
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        syncer.backfill(
            QueryBuilder(), project="p", sync_id="bf",
            start=datetime.now(timezone.utc) - timedelta(hours=1),
            end=far_future,
            window_hours=1,
        )

        # A subsequent sync() must NOT skip — it should fetch new logs
        new_entry = _make_entry("new1", datetime.now(timezone.utc).isoformat())
        client.iter.return_value = iter([new_entry])
        result = syncer.sync(QueryBuilder(), project="p", sync_id="bf")

        assert result.fetched == 1    # not skipped
        assert result.stored == 1

    def test_stores_entries_across_windows(self) -> None:
        e1 = _make_entry("w1", "2026-06-01T03:00:00+00:00")
        e2 = _make_entry("w2", "2026-06-01T09:00:00+00:00")

        client = MagicMock()
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        # Return e1 for first window, e2 for second
        client.iter.side_effect = [iter([e1]), iter([e2]), iter([]), iter([])]

        syncer.backfill(
            QueryBuilder(),
            project="p",
            sync_id="bf",
            start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 2, tzinfo=timezone.utc),
            window_hours=6,
        )
        assert store.count() == 2

    def test_watermark_advances_to_end(self) -> None:
        client = MagicMock()
        client.iter.return_value = iter([])
        store = SQLiteStore(":memory:")
        syncer = Syncer(client, store)

        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        syncer.backfill(
            QueryBuilder(), project="p", sync_id="bf",
            start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            end=end,
            window_hours=6,
        )
        watermark = store.get_watermark("bf")
        assert watermark is not None
        assert abs((watermark - end).total_seconds()) < 2
