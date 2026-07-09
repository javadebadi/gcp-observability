"""
Incremental sync engine — pulls logs from Cloud Logging into local storage.

How it works (the "Airflow-like" part, in ~60 lines):
  1. Read watermark  → the timestamp of the last successful sync
  2. Fetch logs      → Cloud Logging query restricted to (watermark, now]
  3. Store entries   → INSERT OR IGNORE (insert_id PK prevents duplicates)
  4. Update watermark + entry count in one transaction

If step 3 or 4 fails, the watermark is NOT updated, so the next run
re-fetches the same window. INSERT OR IGNORE makes this safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from .logging.client import Client
from .logging.query import QueryBuilder
from .storage.sqlite import SQLiteStore


@dataclass
class SyncResult:
    sync_id: str
    fetched: int        # entries returned by Cloud Logging
    stored: int         # entries actually written (new)
    duplicates: int     # entries skipped (already in store)
    since: datetime     # start of the fetched window
    until: datetime     # end of the fetched window (new watermark)

    def __str__(self) -> str:
        return (
            f"[{self.sync_id}] fetched={self.fetched} "
            f"stored={self.stored} duplicates={self.duplicates} "
            f"window={self.since.isoformat()} → {self.until.isoformat()}"
        )


class Syncer:
    """
    Incremental log syncer.

    Args:
        client:          Authenticated Cloud Logging client.
        store:           Local SQLite store to write into.
        default_lookback: How far back to start on the very first sync
                          (when no watermark exists). Default: 24 hours.

    Example::

        client = Client()
        store  = SQLiteStore("logs.db")
        syncer = Syncer(client, store)

        # Run once (or put this in a cron job / Cloud Scheduler)
        result = syncer.sync(
            query=QueryBuilder().severity_gte("ERROR"),
            project="my-gcp-project",
            sync_id="my-project-errors",
        )
        print(result)

        # Query locally — no Cloud Logging charges
        entries = store.query(severity_gte="ERROR", limit=100)
    """

    def __init__(
        self,
        client: Client,
        store: SQLiteStore,
        default_lookback: timedelta = timedelta(hours=24),
    ) -> None:
        self._client = client
        self._store = store
        self._default_lookback = default_lookback

    def sync(
        self,
        query: Union[QueryBuilder, str],
        *,
        project: Optional[str] = None,
        projects: Optional[list[str]] = None,
        sync_id: str,
        start: Optional[datetime] = None,
        order_by: str = "timestamp asc",
        max_results: Optional[int] = None,
    ) -> SyncResult:
        """
        Run one incremental sync cycle.

        Args:
            query:       Base filter (without time range — the syncer adds that).
            project:     Single project to query.
            projects:    Multiple projects to query in one call.
            sync_id:     Unique name for this sync job (used as watermark key).
                         Use something descriptive: "prod-errors", "my-project-run-logs".
            start:       Override the start time for this run.
                         Useful for backfilling. Ignored if a watermark exists
                         and `start` is earlier than the watermark.
            order_by:    Passed to Cloud Logging. Keep "timestamp asc" so the
                         watermark always moves forward.
            max_results: Cap entries fetched per run (useful for testing or
                         rate-limit-aware incremental backfills).
        """
        now = datetime.now(timezone.utc)

        # --- 1. Determine the fetch window ---
        watermark = self._store.get_watermark(sync_id)
        if watermark and watermark > now:
            # Watermark is in the future (e.g. backfill was run with a future
            # end date). Nothing to fetch yet — skip without moving the watermark.
            return SyncResult(
                sync_id=sync_id,
                fetched=0,
                stored=0,
                duplicates=0,
                since=now,
                until=now,
            )
        if watermark:
            since = watermark
        elif start:
            since = _ensure_utc(start)
        else:
            since = now - self._default_lookback

        # --- 2. Build the full query with time window ---
        base_filter = query.build() if isinstance(query, QueryBuilder) else query
        time_filter = QueryBuilder().time_range(since, now).build()
        full_filter = f"{base_filter}\n{time_filter}".strip() if base_filter else time_filter

        # --- 3. Fetch from Cloud Logging ---
        entries = list(self._client.iter(
            full_filter,
            project=project,
            projects=projects,
            order_by=order_by,
            max_results=max_results,
        ))

        # --- 4. Store + update watermark atomically ---
        stored = self._store.save(entries)
        self._store.set_watermark(sync_id, now, entries_added=stored)

        return SyncResult(
            sync_id=sync_id,
            fetched=len(entries),
            stored=stored,
            duplicates=len(entries) - stored,
            since=since,
            until=now,
        )

    def backfill(
        self,
        query: Union[QueryBuilder, str],
        *,
        project: Optional[str] = None,
        projects: Optional[list[str]] = None,
        sync_id: str,
        start: datetime,
        end: Optional[datetime] = None,
        window_hours: int = 6,
        max_results_per_window: Optional[int] = None,
    ) -> list[SyncResult]:
        """
        Backfill historical logs in fixed-size time windows.

        Splits the [start, end] range into chunks of `window_hours` and syncs
        each chunk separately. This avoids hitting Cloud Logging's response
        size limits and keeps memory usage predictable.

        Args:
            start:                  Beginning of the backfill range.
            end:                    End of the backfill range (default: now).
            window_hours:           Size of each chunk in hours (default: 6).
            max_results_per_window: Optional cap per window.
        """
        now = datetime.now(timezone.utc)
        end = _ensure_utc(end) if end else now
        # Cap end at now — future windows have no logs and would push the
        # watermark into the future, causing sync() to miss logs later.
        if end > now:
            end = now
        start = _ensure_utc(start)
        window = timedelta(hours=window_hours)

        results: list[SyncResult] = []
        cursor = start
        while cursor < end:
            window_end = min(cursor + window, end)

            base_filter = query.build() if isinstance(query, QueryBuilder) else query
            time_filter = QueryBuilder().time_range(cursor, window_end).build()
            full_filter = f"{base_filter}\n{time_filter}".strip() if base_filter else time_filter

            entries = list(self._client.iter(
                full_filter,
                project=project,
                projects=projects,
                order_by="timestamp asc",
                max_results=max_results_per_window,
            ))

            stored = self._store.save(entries)
            # For backfill, only advance the watermark if this window is more
            # recent than the existing watermark (don't regress it).
            existing = self._store.get_watermark(sync_id)
            if existing is None or window_end > existing:
                self._store.set_watermark(sync_id, window_end, entries_added=stored)

            results.append(SyncResult(
                sync_id=sync_id,
                fetched=len(entries),
                stored=stored,
                duplicates=len(entries) - stored,
                since=cursor,
                until=window_end,
            ))
            cursor = window_end

        return results


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
