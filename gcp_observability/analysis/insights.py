"""
Insights — Stage 2 of the analysis pipeline.

Operates on lists of extracted records (``list[dict]``) produced by Stage 1
(``Pipeline``, ``RegexExtractor``, ``JsonExtractor``) to produce filtered
views, groupings, and structured summaries.

Typical flow::

    # Stage 1 — extract
    timeline = pipeline.run(store.query(...))

    # Stage 2 — insights
    from gcp_observability.analysis import insights

    jobs   = insights.group_by(timeline, by="job_id")
    counts = insights.count_by(timeline, by="job_id")
    top    = insights.top_n(timeline, by="job_id", n=5)

    for job_id, events in jobs.items():
        summary = insights.summarize_job(events)
        print(job_id, summary)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


# ── Filtering ──────────────────────────────────────────────────────────────────


def filter_by(records: list[dict], **kwargs: Any) -> list[dict]:
    """
    Return records where every keyword argument matches the corresponding field.

    Example::

        filter_by(timeline, job_id="job_001")
        filter_by(timeline, _source="failed", status="failed")
    """
    result = []
    for rec in records:
        if all(rec.get(k) == v for k, v in kwargs.items()):
            result.append(rec)
    return result


# ── Grouping ───────────────────────────────────────────────────────────────────


def group_by(records: list[dict], by: str) -> dict[str, list[dict]]:
    """
    Group records by the value of *by*, preserving insertion order within
    each group.

    Records missing the field are grouped under the key ``"__missing__"``.

    Example::

        jobs = group_by(timeline, by="job_id")
        # {"job_001": [...events...], "job_002": [...events...]}
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = str(rec.get(by, "__missing__"))
        groups.setdefault(key, []).append(rec)
    return groups


def count_by(records: list[dict], by: str) -> dict[str, int]:
    """
    Count records per unique value of *by*, sorted by count descending.

    Example::

        count_by(records, by="player_id")
        # {"player_42": 15, "player_7": 9, ...}
    """
    counts: dict[str, int] = {}
    for rec in records:
        key = str(rec.get(by, "__missing__"))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def top_n(records: list[dict], by: str, n: int = 10) -> list[tuple[str, int]]:
    """
    Return the top *n* values of *by* ranked by record count.

    Example::

        top_n(records, by="player_id", n=5)
        # [("player_42", 15), ("player_7", 9), ...]
    """
    return list(count_by(records, by).items())[:n]


# ── Lifecycle summary ──────────────────────────────────────────────────────────


def summarize_job(
    events: list[dict],
    *,
    start_source: str = "started",
    end_source: str = "finished",
    step_source: str = "step",
    step_name_field: str = "step",
    duration_field: str = "duration",
    status_field: str = "status",
    total_duration_field: str = "total_duration",
) -> dict:
    """
    Summarize a job lifecycle from a flat list of pipeline events.

    Expects events produced by a ``Pipeline`` with (at least) three source
    names: one for job start, one for each step, and one for job end.
    All arguments have defaults that match the job_lifecycle_tracker example.

    Args:
        events:               Events for a single job (already filtered /
                              grouped by job_id).
        start_source:         ``_source`` value that marks job start.
        end_source:           ``_source`` value that marks job end.
        step_source:          ``_source`` value that marks individual steps.
        step_name_field:      Field in step events that holds the step name.
        duration_field:       Field in step events that holds duration (seconds
                              as a numeric string, e.g. ``"5"``).
        status_field:         Field in end events that holds final status.
        total_duration_field: Field in end events that holds total duration.

    Returns:
        A dict with keys:

        - ``started_at``       — ``datetime`` of the start event, or ``None``
        - ``finished_at``      — ``datetime`` of the end event, or ``None``
        - ``wall_time_s``      — seconds between start and end, or ``None``
        - ``status``           — value of *status_field* from end event
        - ``total_duration_s`` — value of *total_duration_field* (int), or ``None``
        - ``steps``            — ``{step_name: duration_s}`` from step events
        - ``step_count``       — number of step events
        - ``event_count``      — total events passed in

    Example::

        jobs = group_by(timeline, by="job_id")
        for job_id, events in jobs.items():
            s = summarize_job(events)
            print(f"{job_id}: {s['status']} in {s['wall_time_s']:.1f}s")
            for step, dur in s["steps"].items():
                print(f"  {step}: {dur}s")
    """
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: Optional[str] = None
    total_duration_s: Optional[int] = None
    steps: dict[str, int] = {}

    for event in events:
        src = event.get("_source")

        if src == start_source and started_at is None:
            started_at = event.get("_timestamp")

        elif src == end_source and finished_at is None:
            finished_at = event.get("_timestamp")
            raw_status = event.get(status_field)
            status = str(raw_status) if raw_status is not None else None
            raw_total = event.get(total_duration_field)
            if raw_total is not None:
                try:
                    total_duration_s = int(raw_total)
                except (ValueError, TypeError):
                    pass

        elif src == step_source:
            name = event.get(step_name_field)
            raw_dur = event.get(duration_field)
            if name is not None and raw_dur is not None:
                try:
                    steps[str(name)] = int(raw_dur)
                except (ValueError, TypeError):
                    pass

    wall_time_s: Optional[float] = None
    if started_at is not None and finished_at is not None:
        wall_time_s = (finished_at - started_at).total_seconds()

    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_time_s": wall_time_s,
        "status": status,
        "total_duration_s": total_duration_s,
        "steps": steps,
        "step_count": len(steps),
        "event_count": len(events),
    }
