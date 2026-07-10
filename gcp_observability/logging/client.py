"""
Cloud Logging fetch client.

Wraps google-cloud-logging to execute QueryBuilder filters and return
structured LogEntry results. One client instance works across multiple
projects — credentials are held at the client level, projects are
specified per fetch call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Union

from google.cloud.logging import Client as _GCPClient

from .query import QueryBuilder


@dataclass
class LogEntry:
    """Simplified, serialisable representation of a Cloud Logging entry."""

    log_name: str
    severity: str
    timestamp: datetime
    payload: Any  # str | dict depending on log type
    payload_type: str  # "text" | "json" | "proto"
    resource_type: str
    resource_labels: dict[str, str]
    labels: dict[str, str]
    insert_id: str
    trace: Optional[str] = None
    span_id: Optional[str] = None
    http_request: Optional[dict] = field(default=None)
    raw: Any = field(default=None, repr=False)  # original GCP entry

    @property
    def project(self) -> str:
        """Extract project ID from logName."""
        # logName format: projects/{project}/logs/{log_id}
        parts = self.log_name.split("/")
        return parts[1] if len(parts) >= 2 else ""

    @property
    def log_id(self) -> str:
        """Extract log ID from logName (URL-decoded)."""
        parts = self.log_name.split("/logs/", 1)
        if len(parts) == 2:
            from urllib.parse import unquote

            return unquote(parts[1])
        return ""

    def to_dict(self) -> dict:
        return {
            "log_name": self.log_name,
            "severity": self.severity,
            "timestamp": _to_utc(self.timestamp).isoformat().replace("+00:00", "Z"),
            "payload": self.payload,
            "payload_type": self.payload_type,
            "resource_type": self.resource_type,
            "resource_labels": self.resource_labels,
            "labels": self.labels,
            "insert_id": self.insert_id,
            "trace": self.trace,
            "span_id": self.span_id,
            "http_request": self.http_request,
        }


def _parse_entry(entry: Any) -> LogEntry:
    """Convert a raw GCP log entry to a LogEntry."""
    # Payload
    if hasattr(entry, "payload") and isinstance(entry.payload, dict):
        payload = entry.payload
        payload_type = "json"
    elif hasattr(entry, "payload") and isinstance(entry.payload, str):
        payload = entry.payload
        payload_type = "text"
    else:
        payload = str(entry.payload) if hasattr(entry, "payload") else None
        payload_type = "proto"

    # HTTP request
    http_request = None
    if hasattr(entry, "http_request") and entry.http_request:
        req = entry.http_request
        http_request = {
            "method": getattr(req, "request_method", None),
            "url": getattr(req, "request_url", None),
            "status": getattr(req, "status", None),
            "latency": str(getattr(req, "latency", None)),
            "user_agent": getattr(req, "user_agent", None),
        }

    return LogEntry(
        log_name=entry.log_name or "",
        severity=str(entry.severity) if entry.severity else "DEFAULT",
        timestamp=_to_utc(entry.timestamp),
        payload=payload,
        payload_type=payload_type,
        resource_type=entry.resource.type if entry.resource else "",
        resource_labels=dict(entry.resource.labels) if entry.resource else {},
        labels=dict(entry.labels) if entry.labels else {},
        insert_id=entry.insert_id or "",
        trace=getattr(entry, "trace", None),
        span_id=getattr(entry, "span_id", None),
        http_request=http_request,
        raw=entry,
    )


class Client:
    """
    Cloud Logging client for fetching log entries.

    Authentication uses Application Default Credentials (ADC). Run
    `gcloud auth application-default login` if not already configured.

    Args:
        project:  Optional default project ID used when fetch() is called
                  without an explicit project/projects argument.
        page_size: Number of entries to request per API page (default 1000).
    """

    def __init__(
        self,
        project: Optional[str] = None,
        page_size: int = 1000,
    ) -> None:
        self._default_project = project
        self._page_size = page_size
        # GCP client requires a project for initialisation; use the default
        # project or a placeholder — the actual projects queried are passed
        # per fetch() call via the `projects` parameter.
        init_project = project or "placeholder"
        self._client = _GCPClient(project=init_project)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def fetch(
        self,
        query: Union[QueryBuilder, str],
        *,
        project: Optional[str] = None,
        projects: Optional[list[str]] = None,
        order_by: str = "timestamp desc",
        max_results: Optional[int] = None,
    ) -> list[LogEntry]:
        """
        Fetch log entries matching the query and return them as a list.

        Args:
            query:       A QueryBuilder or raw filter string.
            project:     Single project ID to query. Mutually exclusive with
                         `projects`.
            projects:    List of project IDs to query in one call. If neither
                         project nor projects is given, falls back to the
                         default project set at client init.
            order_by:    "timestamp desc" (newest first) or "timestamp asc".
            max_results: Cap the total number of entries returned.
        """
        return list(
            self.iter(
                query,
                project=project,
                projects=projects,
                order_by=order_by,
                max_results=max_results,
            )
        )

    def iter(
        self,
        query: Union[QueryBuilder, str],
        *,
        project: Optional[str] = None,
        projects: Optional[list[str]] = None,
        order_by: str = "timestamp desc",
        max_results: Optional[int] = None,
    ) -> Iterator[LogEntry]:
        """
        Stream log entries one at a time (memory-efficient for large result
        sets). Same arguments as fetch().
        """
        filter_str = query.build() if isinstance(query, QueryBuilder) else query
        project_list = self._resolve_projects(project, projects)

        kwargs: dict[str, Any] = {
            "filter_": filter_str,
            "order_by": order_by,
            "page_size": self._page_size,
        }
        if project_list:
            kwargs["resource_names"] = [f"projects/{p}" for p in project_list]

        count = 0
        for entry in self._client.list_entries(**kwargs):
            yield _parse_entry(entry)
            count += 1
            if max_results is not None and count >= max_results:
                break

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _resolve_projects(
        self,
        project: Optional[str],
        projects: Optional[list[str]],
    ) -> list[str]:
        if project and projects:
            raise ValueError("Pass either `project` or `projects`, not both.")
        if project:
            return [project]
        if projects:
            return projects
        if self._default_project:
            return [self._default_project]
        return []


def _to_utc(dt: datetime) -> datetime:
    """Ensure a datetime is UTC-aware. Naïve datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
