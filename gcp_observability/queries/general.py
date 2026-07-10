"""
Ready-to-use QueryBuilder presets for common cross-service log scenarios.

Each function returns a QueryBuilder so you can chain any additional
filters (.since(), .time_range(), .project(), etc.) on top.

Example::

    from gcp_observability.queries import general

    # Errors in the last hour
    q = general.errors().since(hours=1)

    # Slow HTTP requests in a specific window
    q = general.http_slow(2.0).time_range("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z")

    # Combine with project
    q = general.errors().project("my-gcp-project")
"""

from __future__ import annotations

from ..logging.constants import Severity
from ..logging.query import QueryBuilder


def errors() -> QueryBuilder:
    """ERROR severity and above (ERROR, CRITICAL, ALERT, EMERGENCY)."""
    return QueryBuilder().severity_gte(Severity.ERROR)


def warnings() -> QueryBuilder:
    """WARNING severity and above."""
    return QueryBuilder().severity_gte(Severity.WARNING)


def critical() -> QueryBuilder:
    """CRITICAL severity and above (CRITICAL, ALERT, EMERGENCY)."""
    return QueryBuilder().severity_gte(Severity.CRITICAL)


def info() -> QueryBuilder:
    """INFO severity and above — excludes DEBUG and DEFAULT."""
    return QueryBuilder().severity_gte(Severity.INFO)


def text_search(query: str) -> QueryBuilder:
    """
    Search across all log fields simultaneously (textPayload, jsonPayload,
    protoPayload, labels, etc.).

    Example::

        general.text_search("ValueError: division by zero").since(hours=6)
    """
    return QueryBuilder().global_search(query)


def http_errors(status_gte: int = 500) -> QueryBuilder:
    """
    HTTP responses with status >= status_gte.

    Args:
        status_gte: Minimum HTTP status code.
                    500 → server errors only (default).
                    400 → client + server errors.
    """
    return QueryBuilder().http_status(">=", status_gte)


def http_slow(seconds: float) -> QueryBuilder:
    """
    HTTP requests that took at least `seconds` seconds.

    Example::

        general.http_slow(2.0).since(hours=24)
    """
    return QueryBuilder().http_latency_gte(seconds)


def by_trace(trace_id: str) -> QueryBuilder:
    """
    All log entries tied to a specific Cloud Trace ID.

    Useful for correlating logs across services for one request.
    """
    return QueryBuilder().trace(trace_id)


def by_label(key: str, value: str) -> QueryBuilder:
    """
    Logs carrying a specific label key-value pair.

    Example::

        general.by_label("env", "production").severity_gte("ERROR")
    """
    return QueryBuilder().label(key, value)
