"""
SQLite-backed store for Cloud Logging entries and sync watermarks.

Design decisions:
- insert_id is the PRIMARY KEY — INSERT OR IGNORE makes all writes idempotent.
- Payload is JSON-encoded so dicts and strings are stored uniformly.
- severity_level (int) is stored alongside severity (str) for fast gte queries.
- WAL mode is enabled so reads never block writes.
- All watermark + entry writes in a sync happen in one transaction so a crash
  mid-sync leaves the watermark unchanged and the next run re-fetches cleanly.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

from ..logging.client import LogEntry
from ..logging.constants import PayloadType

# Numeric severity levels matching Cloud Logging's own ordering.
_SEVERITY_LEVEL: dict[str, int] = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
    "ALERT": 700,
    "EMERGENCY": 800,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_entries (
    insert_id        TEXT PRIMARY KEY,
    log_name         TEXT NOT NULL,
    project          TEXT NOT NULL,
    log_id           TEXT NOT NULL,
    severity         TEXT NOT NULL,
    severity_level   INTEGER NOT NULL,
    timestamp        TEXT NOT NULL,
    payload_type     TEXT NOT NULL,
    payload          TEXT,
    resource_type    TEXT,
    resource_labels  TEXT,
    labels           TEXT,
    trace            TEXT,
    span_id          TEXT,
    http_request     TEXT,
    synced_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timestamp       ON log_entries(timestamp);
CREATE INDEX IF NOT EXISTS idx_severity_level  ON log_entries(severity_level);
CREATE INDEX IF NOT EXISTS idx_project         ON log_entries(project);
CREATE INDEX IF NOT EXISTS idx_log_name        ON log_entries(log_name);
CREATE INDEX IF NOT EXISTS idx_resource_type   ON log_entries(resource_type);

CREATE TABLE IF NOT EXISTS sync_state (
    sync_id              TEXT PRIMARY KEY,
    last_synced_at       TEXT,
    last_run_at          TEXT,
    total_entries_synced INTEGER NOT NULL DEFAULT 0
);
"""


class SQLiteStore:
    """
    Local SQLite store for log entries and sync state.

    Args:
        path: File path for the SQLite database.
              Use ":memory:" for in-process testing.
    """

    def __init__(self, path: str = "logs.db") -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Entries                                                              #
    # ------------------------------------------------------------------ #

    def save(self, entries: list[LogEntry]) -> int:
        """
        Persist entries. Duplicates (same insert_id) are silently skipped.
        Returns the number of rows actually inserted.
        """
        if not entries:
            return 0
        synced_at = _now_iso()
        rows = [_entry_to_row(e, synced_at) for e in entries]
        with self._transaction() as cur:
            cur.executemany(
                """
                INSERT OR IGNORE INTO log_entries (
                    insert_id, log_name, project, log_id,
                    severity, severity_level, timestamp,
                    payload_type, payload,
                    resource_type, resource_labels, labels,
                    trace, span_id, http_request, synced_at
                ) VALUES (
                    :insert_id, :log_name, :project, :log_id,
                    :severity, :severity_level, :timestamp,
                    :payload_type, :payload,
                    :resource_type, :resource_labels, :labels,
                    :trace, :span_id, :http_request, :synced_at
                )
                """,
                rows,
            )
            return cur.rowcount

    def query(
        self,
        *,
        project: Optional[str] = None,
        log_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        severity_gte: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        search: Optional[str] = None,
        limit: int = 1000,
        order: str = "desc",
    ) -> list[LogEntry]:
        """
        Query locally stored log entries.

        Args:
            project:       Filter by GCP project ID.
            log_id:        Filter by log ID (e.g. "cloudrun.googleapis.com/requests").
            resource_type: Filter by resource type.
            severity_gte:  Minimum severity (e.g. "ERROR" — returns ERROR and above).
            start:         Earliest timestamp (inclusive).
            end:           Latest timestamp (exclusive).
            search:        Substring search across the payload text.
            limit:         Maximum rows returned (default 1000).
            order:         "desc" (newest first) or "asc" (oldest first).
        """
        clauses: list[str] = []
        params: list[object] = []

        if project:
            clauses.append("project = ?")
            params.append(project)
        if log_id:
            clauses.append("log_id = ?")
            params.append(log_id)
        if resource_type:
            clauses.append("resource_type = ?")
            params.append(resource_type)
        if severity_gte:
            level = _SEVERITY_LEVEL.get(severity_gte.upper(), 0)
            clauses.append("severity_level >= ?")
            params.append(level)
        if start:
            clauses.append("timestamp >= ?")
            params.append(_to_iso(start))
        if end:
            clauses.append("timestamp < ?")
            params.append(_to_iso(end))
        if search:
            clauses.append("payload LIKE ?")
            params.append(f"%{search}%")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "DESC" if order.lower() == "desc" else "ASC"
        sql = (
            f"SELECT * FROM log_entries {where} ORDER BY timestamp {direction} LIMIT ?"
        )
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def count(self) -> int:
        """Total number of stored log entries."""
        return self._conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Watermark / sync state                                               #
    # ------------------------------------------------------------------ #

    def get_watermark(self, sync_id: str) -> Optional[datetime]:
        """Return the last synced timestamp for a sync job, or None if never run."""
        row = self._conn.execute(
            "SELECT last_synced_at FROM sync_state WHERE sync_id = ?", (sync_id,)
        ).fetchone()
        if row and row["last_synced_at"]:
            dt = datetime.fromisoformat(row["last_synced_at"])
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return None

    def set_watermark(
        self,
        sync_id: str,
        watermark: datetime,
        entries_added: int = 0,
    ) -> None:
        """Update the watermark for a sync job."""
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO sync_state (sync_id, last_synced_at, last_run_at, total_entries_synced)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sync_id) DO UPDATE SET
                    last_synced_at = excluded.last_synced_at,
                    last_run_at    = excluded.last_run_at,
                    total_entries_synced = total_entries_synced + excluded.total_entries_synced
                """,
                (sync_id, _to_iso(watermark), _now_iso(), entries_added),
            )

    def list_sync_jobs(self) -> list[dict]:
        """Return all known sync jobs and their state."""
        rows = self._conn.execute(
            "SELECT * FROM sync_state ORDER BY last_run_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _parse_utc(s: str) -> datetime:
    """Parse an ISO string, normalise to UTC, strip sub-second if needed."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _entry_to_row(entry: LogEntry, synced_at: str) -> dict:
    payload = (
        json.dumps(entry.payload) if isinstance(entry.payload, dict) else entry.payload
    )
    return {
        "insert_id": entry.insert_id or f"_no_id_{entry.timestamp.isoformat()}",
        "log_name": entry.log_name,
        "project": entry.project,
        "log_id": entry.log_id,
        "severity": entry.severity,
        "severity_level": _SEVERITY_LEVEL.get(entry.severity.upper(), 0),
        "timestamp": _to_iso(entry.timestamp),
        "payload_type": entry.payload_type,
        "payload": payload,
        "resource_type": entry.resource_type,
        "resource_labels": json.dumps(entry.resource_labels),
        "labels": json.dumps(entry.labels),
        "trace": entry.trace,
        "span_id": entry.span_id,
        "http_request": json.dumps(entry.http_request) if entry.http_request else None,
        "synced_at": synced_at,
    }


def _row_to_entry(row: sqlite3.Row) -> LogEntry:
    payload_raw = row["payload"]
    payload_type = PayloadType(row["payload_type"])
    if payload_type == PayloadType.JSON and payload_raw:
        try:
            payload: object = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = payload_raw
    else:
        payload = payload_raw

    http_request = None
    if row["http_request"]:
        try:
            http_request = json.loads(row["http_request"])
        except json.JSONDecodeError:
            pass

    return LogEntry(
        log_name=row["log_name"],
        severity=row["severity"],
        timestamp=_parse_utc(row["timestamp"]),
        payload=payload,
        payload_type=payload_type,
        resource_type=row["resource_type"] or "",
        resource_labels=json.loads(row["resource_labels"] or "{}"),
        labels=json.loads(row["labels"] or "{}"),
        insert_id=row["insert_id"],
        trace=row["trace"],
        span_id=row["span_id"],
        http_request=http_request,
    )
