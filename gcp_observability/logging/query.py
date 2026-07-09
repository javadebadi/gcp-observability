"""
Fluent QueryBuilder for Cloud Logging filter language.

Builds the filter strings accepted by:
  - Cloud Logging console ("Build query" panel)
  - gcloud logging read --filter=...
  - google-cloud-logging client library list_entries(filter_=...)

Usage:
    from gcp_observability.logging import QueryBuilder, Severity, ResourceType

    q = (
        QueryBuilder()
        .resource_type(ResourceType.CLOUD_RUN_REVISION)
        .resource_label("service_name", "my-api")
        .project("my-gcp-project")
        .severity_gte(Severity.ERROR)
        .since(hours=24)
        .json_payload("statusCode", ">=", 500)
        .build()
    )
    print(q)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from .expressions import And, Comparison, Expr, F, Not, Or, Raw


class QueryBuilder:
    def __init__(self) -> None:
        self._filters: list[Expr] = []

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _add(self, expr: Expr) -> QueryBuilder:
        self._filters.append(expr)
        return self

    # ------------------------------------------------------------------ #
    # Resource                                                             #
    # ------------------------------------------------------------------ #

    def resource_type(self, type_name: str) -> QueryBuilder:
        return self._add(F("resource.type") == type_name)

    def resource_label(self, key: str, value: str) -> QueryBuilder:
        return self._add(F("resource.labels")[key] == value)

    # ------------------------------------------------------------------ #
    # Log name / project                                                   #
    # ------------------------------------------------------------------ #

    def log_name(self, name: str) -> QueryBuilder:
        """Exact logName match, e.g. 'projects/my-project/logs/my-log'."""
        return self._add(F("logName") == name)

    def project(self, project_id: str, log_id: Optional[str] = None) -> QueryBuilder:
        """
        Filter to a specific GCP project.

        If log_id is given, matches that exact log; otherwise matches all
        logs in the project via substring match.
        """
        if log_id:
            return self.log_name(f"projects/{project_id}/logs/{log_id}")
        return self._add(F("logName").has(f"projects/{project_id}"))

    # ------------------------------------------------------------------ #
    # Severity                                                             #
    # ------------------------------------------------------------------ #

    def severity(self, op: str, level: str) -> QueryBuilder:
        """
        Severity with explicit operator.

        Args:
            op:    One of =, !=, >, <, >=, <=
            level: Severity name (e.g. "ERROR") or use Severity constants.
        """
        return self._add(Comparison("severity", op, level))

    def severity_eq(self, level: str) -> QueryBuilder:
        return self.severity("=", level)

    def severity_gte(self, level: str) -> QueryBuilder:
        """Match this severity level and above (e.g. >=ERROR captures CRITICAL too)."""
        return self.severity(">=", level)

    def severity_lte(self, level: str) -> QueryBuilder:
        return self.severity("<=", level)

    def severity_range(self, low: str, high: str) -> QueryBuilder:
        """Match severities between low and high (inclusive)."""
        return (
            self._add(Comparison("severity", ">=", low))
                ._add(Comparison("severity", "<=", high))
        )

    # ------------------------------------------------------------------ #
    # Timestamp                                                            #
    # ------------------------------------------------------------------ #

    def time_range(
        self,
        start: Union[str, datetime],
        end: Optional[Union[str, datetime]] = None,
    ) -> QueryBuilder:
        """
        Filter by timestamp window.

        Args:
            start: ISO-8601 string or datetime (UTC assumed if naïve).
            end:   ISO-8601 string or datetime; open-ended if omitted.
        """
        start_str = _to_iso(start)
        self._add(F("timestamp") >= start_str)
        if end is not None:
            self._add(F("timestamp") < _to_iso(end))
        return self

    def since(self, hours: float = 0, minutes: float = 0, days: float = 0) -> QueryBuilder:
        """Shorthand: logs from the last N hours/minutes/days until now."""
        delta = timedelta(hours=hours, minutes=minutes, days=days)
        start = datetime.now(timezone.utc) - delta
        return self._add(F("timestamp") >= _to_iso(start))

    # ------------------------------------------------------------------ #
    # Payload                                                              #
    # ------------------------------------------------------------------ #

    def text_payload(self, value: str, exact: bool = False) -> QueryBuilder:
        """
        Filter on textPayload.

        Args:
            exact: If True, use = (exact match). Default is : (substring).
        """
        op = "=" if exact else ":"
        return self._add(Comparison("textPayload", op, value))

    def json_payload(self, field: str, op: str, value: object) -> QueryBuilder:
        """
        Filter on a jsonPayload sub-field.

        Example:
            .json_payload("level", "=", "error")
            .json_payload("statusCode", ">=", 500)
        """
        return self._add(Comparison(f"jsonPayload.{field}", op, value))

    def json_payload_has(self, field: str, value: str) -> QueryBuilder:
        """Substring match on a jsonPayload sub-field."""
        return self._add(Comparison(f"jsonPayload.{field}", ":", value))

    def proto_payload(self, field: str, op: str, value: object) -> QueryBuilder:
        return self._add(Comparison(f"protoPayload.{field}", op, value))

    # ------------------------------------------------------------------ #
    # HTTP request                                                         #
    # ------------------------------------------------------------------ #

    def http_method(self, method: str) -> QueryBuilder:
        return self._add(F("httpRequest.requestMethod") == method.upper())

    def http_status(self, op: str, status: int) -> QueryBuilder:
        return self._add(Comparison("httpRequest.status", op, status))

    def http_url(self, value: str, exact: bool = False) -> QueryBuilder:
        op = "=" if exact else ":"
        return self._add(Comparison("httpRequest.requestUrl", op, value))

    def http_latency_gte(self, seconds: float) -> QueryBuilder:
        """Requests that took at least `seconds` seconds."""
        return self._add(Comparison("httpRequest.latency", ">=", f"{seconds}s"))

    # ------------------------------------------------------------------ #
    # Labels                                                               #
    # ------------------------------------------------------------------ #

    def label(self, key: str, value: str) -> QueryBuilder:
        """
        Match a log entry label.

        Keys with special characters (dots, hyphens) are automatically quoted
        in the filter, e.g. labels."k8s-pod/app"="my-service".
        """
        return self._add(F("labels")[key] == value)

    # ------------------------------------------------------------------ #
    # Trace / span                                                         #
    # ------------------------------------------------------------------ #

    def trace(self, trace_id: str, exact: bool = False) -> QueryBuilder:
        op = "=" if exact else ":"
        return self._add(Comparison("trace", op, trace_id))

    def span_id(self, span_id: str) -> QueryBuilder:
        return self._add(F("spanId") == span_id)

    def sampled(self, value: bool = True) -> QueryBuilder:
        return self._add(F("traceSampled") == value)

    # ------------------------------------------------------------------ #
    # Operation                                                            #
    # ------------------------------------------------------------------ #

    def operation_id(self, op_id: str) -> QueryBuilder:
        return self._add(F("operation.id") == op_id)

    def operation_producer(self, producer: str) -> QueryBuilder:
        return self._add(F("operation.producer").has(producer))

    # ------------------------------------------------------------------ #
    # Other standard fields                                                #
    # ------------------------------------------------------------------ #

    def insert_id(self, id: str) -> QueryBuilder:
        return self._add(F("insertId") == id)

    def source_location(self, file: Optional[str] = None, function: Optional[str] = None) -> QueryBuilder:
        if file:
            self._add(F("sourceLocation.file").has(file))
        if function:
            self._add(F("sourceLocation.function").has(function))
        return self

    # ------------------------------------------------------------------ #
    # Arbitrary expressions                                                #
    # ------------------------------------------------------------------ #

    def where(self, expr: Expr) -> QueryBuilder:
        """Add a raw Expr (built with F() / operators) to the query."""
        return self._add(expr)

    def raw(self, filter_str: str) -> QueryBuilder:
        """Add a raw filter string verbatim."""
        return self._add(Raw(filter_str))

    # ------------------------------------------------------------------ #
    # Build                                                                #
    # ------------------------------------------------------------------ #

    def build(self) -> str:
        """Return the complete Cloud Logging filter string."""
        if not self._filters:
            return ""
        return "\n".join(f.build() for f in self._filters)

    def __str__(self) -> str:
        return self.build()

    def __repr__(self) -> str:
        return f"QueryBuilder({self.build()!r})"


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _to_iso(dt: Union[str, datetime]) -> str:
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")
