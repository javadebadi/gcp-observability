"""
Detect which Cloud Run services have trace IDs in their application logs.

A service "has traces" if its application logs (stdout/stderr from the container)
carry a trace field matching the platform request log. This is set by OTEL or any
structured logging library that propagates W3C trace context.

A service "is dark" if:
  - It emits no application logs at all, OR
  - Its application logs have no trace field (logs exist but trace context is dropped)

Output per service:
  - sample_requests: how many platform request logs were sampled
  - app_logs_with_trace: how many of those traces had correlated app logs
  - app_logs_without_trace: app logs exist but no trace field
  - no_app_logs: requests with zero app log correlation found
  - verdict: OK | NO_TRACE | DARK
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.cloud import logging_v2


@dataclass
class ServiceTraceCoverage:
    project: str
    service: str
    region: str
    sample_requests: int
    app_logs_with_trace: int
    no_app_logs: int          # platform request had no correlated app logs at all
    app_logs_without_trace: int  # app logs found but trace field missing
    verdict: str              # OK | NO_TRACE | DARK
    example_missing_trace_ids: list[str]  # a few trace IDs for manual inspection


def _request_logs(
    client: logging_v2.Client,
    project: str,
    service: str,
    hours_back: int,
    sample_size: int,
) -> list[tuple[str, str]]:
    """
    Return up to sample_size (trace_id, log_name) pairs from platform request logs.
    Platform request logs always have httpRequest set and a trace field (GCP auto-injects).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    filt = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'resource.labels.project_id="{project}" '
        f'httpRequest.requestUrl!="" '
        f'trace!="" '
        f'timestamp>="{since}"'
    )
    results = []
    try:
        for entry in client.list_log_entries(
            resource_names=[f"projects/{project}"],
            filter_=filt,
            order_by="timestamp desc",
            page_size=sample_size,
            max_results=sample_size,
        ):
            if entry.trace:
                results.append((entry.trace, entry.log_name))
    except Exception as e:
        print(f"  [warn] Could not query request logs for {service} in {project}: {e}")
    return results


def _has_app_logs_with_trace(
    client: logging_v2.Client,
    project: str,
    service: str,
    trace_id: str,
) -> tuple[bool, bool]:
    """
    Check if a specific trace has correlated application logs.
    Returns (has_app_logs, has_trace_field).
      has_app_logs=False → no app logs found at all for this trace
      has_app_logs=True, has_trace_field=True → OTEL / trace propagation working
      has_app_logs=True, has_trace_field=False → app logs exist but trace dropped
    """
    filt = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'resource.labels.project_id="{project}" '
        f'trace="{trace_id}" '
        f'httpRequest.requestUrl=""'  # exclude the platform request log itself
    )
    try:
        entries = list(client.list_log_entries(
            resource_names=[f"projects/{project}"],
            filter_=filt,
            max_results=1,
        ))
        if entries:
            return True, bool(entries[0].trace)
        # No app logs found for this trace — check if any app logs exist at all
        # by querying without the trace filter
        filt_any = (
            f'resource.type="cloud_run_revision" '
            f'resource.labels.service_name="{service}" '
            f'resource.labels.project_id="{project}" '
            f'httpRequest.requestUrl=""'
        )
        any_entries = list(client.list_log_entries(
            resource_names=[f"projects/{project}"],
            filter_=filt_any,
            max_results=1,
        ))
        if any_entries:
            # App logs exist for the service but not correlated to this trace
            return True, False
        return False, False
    except Exception as e:
        print(f"  [warn] Could not query app logs for trace {trace_id}: {e}")
        return False, False


def check_trace_coverage(
    project: str,
    service: str,
    region: str,
    hours_back: int = 24,
    sample_size: int = 20,
) -> ServiceTraceCoverage:
    client = logging_v2.Client(project=project)

    request_logs = _request_logs(client, project, service, hours_back, sample_size)

    if not request_logs:
        return ServiceTraceCoverage(
            project=project, service=service, region=region,
            sample_requests=0,
            app_logs_with_trace=0,
            no_app_logs=0,
            app_logs_without_trace=0,
            verdict="NO_DATA",
            example_missing_trace_ids=[],
        )

    app_logs_with_trace = 0
    no_app_logs = 0
    app_logs_without_trace = 0
    missing_examples: list[str] = []

    for trace_id, _ in request_logs:
        has_app, has_trace = _has_app_logs_with_trace(client, project, service, trace_id)
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

    # Verdict
    if app_logs_with_trace == len(request_logs):
        verdict = "OK"
    elif app_logs_with_trace > 0:
        verdict = "PARTIAL"
    elif app_logs_without_trace > 0:
        verdict = "NO_TRACE"   # logs exist but trace field is missing
    else:
        verdict = "DARK"       # no application logs at all

    return ServiceTraceCoverage(
        project=project,
        service=service,
        region=region,
        sample_requests=len(request_logs),
        app_logs_with_trace=app_logs_with_trace,
        no_app_logs=no_app_logs,
        app_logs_without_trace=app_logs_without_trace,
        verdict=verdict,
        example_missing_trace_ids=missing_examples,
    )
