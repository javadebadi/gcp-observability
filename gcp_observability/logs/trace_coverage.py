"""
Detect which Cloud Run services have trace IDs in their application logs.

A service "has traces" if its application logs (stdout/stderr from the container)
carry a trace field matching the platform request log. This is set by OTEL or any
structured logging library that propagates W3C trace context.

Verdicts:
  OK        — all sampled requests have correlated app logs with trace IDs
  PARTIAL   — some requests have traces, some don't
  NO_TRACE  — app logs exist but none carry a trace field
  DARK      — no application logs found at all (service may not log to stdout)
  NO_DATA   — no platform request logs found in the time window
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from google.cloud import logging as gcp_logging


@dataclass
class ServiceTraceCoverage:
    project: str
    service: str
    region: str
    sample_requests: int
    app_logs_with_trace: int
    no_app_logs: int
    app_logs_without_trace: int
    verdict: str
    example_missing_trace_ids: list[str] = field(default_factory=list)


def _make_client(project: str) -> gcp_logging.Client:
    return gcp_logging.Client(project=project)


def _list_entries(
    client: gcp_logging.Client,
    project: str,
    filter_: str,
    max_results: int,
) -> list:
    try:
        return list(client.list_entries(
            resource_names=[f"projects/{project}"],
            filter_=filter_,
            order_by="timestamp desc",
            max_results=max_results,
        ))
    except Exception as e:
        print(f"  [warn] Log query failed: {e}")
        return []


def _request_logs(
    client: gcp_logging.Client,
    project: str,
    service: str,
    hours_back: int,
    sample_size: int,
) -> list[str]:
    """Return up to sample_size trace IDs from platform request logs."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    filter_ = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'httpRequest.requestUrl!="" '
        f'trace!="" '
        f'timestamp>="{since}"'
    )
    entries = _list_entries(client, project, filter_, sample_size)
    return [e.trace for e in entries if e.trace]


def _check_app_logs(
    client: gcp_logging.Client,
    project: str,
    service: str,
    trace_id: str,
) -> tuple[bool, bool]:
    """
    Returns (has_app_logs, has_trace).
    Queries for non-request logs correlated to this trace.
    """
    # App logs for this specific trace
    filter_trace = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'trace="{trace_id}" '
        f'httpRequest.requestUrl=""'
    )
    entries = _list_entries(client, project, filter_trace, 1)
    if entries:
        return True, bool(entries[0].trace)

    # No correlated app logs — check if the service emits any app logs at all
    filter_any = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'httpRequest.requestUrl=""'
    )
    any_entries = _list_entries(client, project, filter_any, 1)
    if any_entries:
        return True, False  # app logs exist but not correlated to this trace

    return False, False


def check_trace_coverage(
    project: str,
    service: str,
    region: str,
    hours_back: int = 24,
    sample_size: int = 20,
) -> ServiceTraceCoverage:
    client = _make_client(project)

    trace_ids = _request_logs(client, project, service, hours_back, sample_size)

    if not trace_ids:
        return ServiceTraceCoverage(
            project=project, service=service, region=region,
            sample_requests=0,
            app_logs_with_trace=0,
            no_app_logs=0,
            app_logs_without_trace=0,
            verdict="NO_DATA",
        )

    app_logs_with_trace = 0
    no_app_logs = 0
    app_logs_without_trace = 0
    missing_examples: list[str] = []

    for trace_id in trace_ids:
        has_app, has_trace = _check_app_logs(client, project, service, trace_id)
        if has_app and has_trace:
            app_logs_with_trace += 1
        elif has_app and not has_trace:
            app_logs_without_trace += 1
            if len(missing_examples) < 3:
                missing_examples.append(trace_id)
        else:
            no_app_logs += 1
            if len(missing_examples) < 3:
                missing_examples.append(trace_id)

    total = len(trace_ids)
    if app_logs_with_trace == total:
        verdict = "OK"
    elif app_logs_with_trace > 0:
        verdict = "PARTIAL"
    elif app_logs_without_trace > 0:
        verdict = "NO_TRACE"
    else:
        verdict = "DARK"

    return ServiceTraceCoverage(
        project=project,
        service=service,
        region=region,
        sample_requests=total,
        app_logs_with_trace=app_logs_with_trace,
        no_app_logs=no_app_logs,
        app_logs_without_trace=app_logs_without_trace,
        verdict=verdict,
        example_missing_trace_ids=missing_examples,
    )
