"""Tests for gcp_observability.analysis.insights."""

from __future__ import annotations

from datetime import datetime, timezone

from gcp_observability.analysis.insights import (
    count_by,
    filter_by,
    group_by,
    summarize_job,
    top_n,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_T1 = datetime(2026, 1, 4, 10, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 1, 4, 10, 5, 0, tzinfo=timezone.utc)
_T3 = datetime(2026, 1, 4, 10, 17, 0, tzinfo=timezone.utc)


def _job_timeline(
    job_id: str = "job_001",
    name: str = "data_export",
    steps: list[tuple[str, int]] | None = None,
    status: str = "success",
    total_duration: int = 17,
    start_ts: datetime = _T1,
    end_ts: datetime = _T3,
) -> list[dict]:
    """Build a synthetic pipeline timeline for one job."""
    if steps is None:
        steps = [("fetch_data", 5), ("transform", 12)]

    events: list[dict] = [
        {
            "_source": "started",
            "_timestamp": start_ts,
            "_severity": "INFO",
            "_insert_id": f"{job_id}-s",
            "_log_name": "projects/test/logs/app",
            "job_id": job_id,
            "name": name,
        }
    ]
    for i, (step_name, dur) in enumerate(steps):
        events.append({
            "_source": "step",
            "_timestamp": _T2,
            "_severity": "INFO",
            "_insert_id": f"{job_id}-step-{i}",
            "_log_name": "projects/test/logs/app",
            "job_id": job_id,
            "step": step_name,
            "duration": str(dur),
        })
    events.append({
        "_source": "finished",
        "_timestamp": end_ts,
        "_severity": "INFO",
        "_insert_id": f"{job_id}-f",
        "_log_name": "projects/test/logs/app",
        "job_id": job_id,
        "status": status,
        "total_duration": str(total_duration),
    })
    return events


# ── filter_by ──────────────────────────────────────────────────────────────────


class TestFilterBy:
    def test_single_field_match(self) -> None:
        records = [{"x": 1}, {"x": 2}, {"x": 1}]
        assert filter_by(records, x=1) == [{"x": 1}, {"x": 1}]

    def test_multiple_fields_all_must_match(self) -> None:
        records = [{"a": 1, "b": 2}, {"a": 1, "b": 3}, {"a": 2, "b": 2}]
        assert filter_by(records, a=1, b=2) == [{"a": 1, "b": 2}]

    def test_no_match_returns_empty(self) -> None:
        assert filter_by([{"x": 1}], x=99) == []

    def test_empty_input(self) -> None:
        assert filter_by([], x=1) == []

    def test_missing_field_does_not_match(self) -> None:
        assert filter_by([{"y": 1}], x=1) == []

    def test_filter_by_source(self) -> None:
        timeline = _job_timeline()
        steps = filter_by(timeline, _source="step")
        assert len(steps) == 2
        assert all(e["_source"] == "step" for e in steps)

    def test_filter_by_job_id(self) -> None:
        timeline = _job_timeline("job_001") + _job_timeline("job_002")
        result = filter_by(timeline, job_id="job_001")
        assert all(e["job_id"] == "job_001" for e in result)
        assert len(result) == 4  # started + 2 steps + finished


# ── group_by ───────────────────────────────────────────────────────────────────


class TestGroupBy:
    def test_basic_grouping(self) -> None:
        records = [{"k": "a"}, {"k": "b"}, {"k": "a"}]
        groups = group_by(records, by="k")
        assert set(groups.keys()) == {"a", "b"}
        assert len(groups["a"]) == 2
        assert len(groups["b"]) == 1

    def test_missing_field_goes_to_missing_key(self) -> None:
        records = [{"k": "a"}, {"other": "x"}]
        groups = group_by(records, by="k")
        assert "__missing__" in groups
        assert len(groups["__missing__"]) == 1

    def test_empty_input(self) -> None:
        assert group_by([], by="k") == {}

    def test_group_by_job_id(self) -> None:
        timeline = _job_timeline("job_001") + _job_timeline("job_002")
        groups = group_by(timeline, by="job_id")
        assert set(groups.keys()) == {"job_001", "job_002"}
        assert len(groups["job_001"]) == 4
        assert len(groups["job_002"]) == 4

    def test_preserves_event_order_within_group(self) -> None:
        records = [{"k": "a", "i": 1}, {"k": "b", "i": 2}, {"k": "a", "i": 3}]
        groups = group_by(records, by="k")
        assert [r["i"] for r in groups["a"]] == [1, 3]


# ── count_by ───────────────────────────────────────────────────────────────────


class TestCountBy:
    def test_basic_count(self) -> None:
        records = [{"k": "a"}, {"k": "b"}, {"k": "a"}, {"k": "a"}]
        counts = count_by(records, by="k")
        assert counts["a"] == 3
        assert counts["b"] == 1

    def test_sorted_descending(self) -> None:
        records = [{"k": "b"}, {"k": "a"}, {"k": "a"}, {"k": "b"}, {"k": "b"}]
        counts = count_by(records, by="k")
        values = list(counts.values())
        assert values == sorted(values, reverse=True)

    def test_empty_input(self) -> None:
        assert count_by([], by="k") == {}

    def test_missing_field_counted(self) -> None:
        records = [{"other": 1}, {"other": 2}]
        counts = count_by(records, by="k")
        assert counts["__missing__"] == 2


# ── top_n ──────────────────────────────────────────────────────────────────────


class TestTopN:
    def test_returns_n_items(self) -> None:
        records = [{"k": str(i % 5)} for i in range(20)]
        result = top_n(records, by="k", n=3)
        assert len(result) == 3

    def test_returns_tuples(self) -> None:
        records = [{"k": "a"}, {"k": "b"}, {"k": "a"}]
        result = top_n(records, by="k", n=2)
        assert result[0] == ("a", 2)
        assert result[1] == ("b", 1)

    def test_n_larger_than_unique_values(self) -> None:
        records = [{"k": "a"}, {"k": "b"}]
        result = top_n(records, by="k", n=10)
        assert len(result) == 2

    def test_default_n_is_10(self) -> None:
        records = [{"k": str(i)} for i in range(15)]
        assert len(top_n(records, by="k")) == 10


# ── summarize_job ──────────────────────────────────────────────────────────────


class TestSummarizeJob:
    def test_basic_summary(self) -> None:
        s = summarize_job(_job_timeline())
        assert s["status"] == "success"
        assert s["total_duration_s"] == 17
        assert s["steps"] == {"fetch_data": 5, "transform": 12}
        assert s["step_count"] == 2
        assert s["event_count"] == 4

    def test_started_at_and_finished_at(self) -> None:
        s = summarize_job(_job_timeline(start_ts=_T1, end_ts=_T3))
        assert s["started_at"] == _T1
        assert s["finished_at"] == _T3

    def test_wall_time_computed(self) -> None:
        s = summarize_job(_job_timeline(start_ts=_T1, end_ts=_T3))
        assert s["wall_time_s"] == (_T3 - _T1).total_seconds()

    def test_failed_job(self) -> None:
        s = summarize_job(_job_timeline(status="failed", total_duration=3, steps=[("query", 3)]))
        assert s["status"] == "failed"
        assert s["total_duration_s"] == 3
        assert s["steps"] == {"query": 3}

    def test_no_started_event(self) -> None:
        events = [e for e in _job_timeline() if e["_source"] != "started"]
        s = summarize_job(events)
        assert s["started_at"] is None
        assert s["wall_time_s"] is None

    def test_no_finished_event(self) -> None:
        events = [e for e in _job_timeline() if e["_source"] != "finished"]
        s = summarize_job(events)
        assert s["finished_at"] is None
        assert s["status"] is None
        assert s["total_duration_s"] is None
        assert s["wall_time_s"] is None

    def test_no_steps(self) -> None:
        s = summarize_job(_job_timeline(steps=[]))
        assert s["steps"] == {}
        assert s["step_count"] == 0

    def test_empty_events(self) -> None:
        s = summarize_job([])
        assert s["started_at"] is None
        assert s["finished_at"] is None
        assert s["status"] is None
        assert s["steps"] == {}
        assert s["event_count"] == 0

    def test_custom_field_names(self) -> None:
        events = [
            {"_source": "begin",  "_timestamp": _T1, "jid": "j1"},
            {"_source": "task",   "_timestamp": _T2, "task_name": "ingest", "dur": "8"},
            {"_source": "end",    "_timestamp": _T3, "result": "ok", "elapsed": "8"},
        ]
        s = summarize_job(
            events,
            start_source="begin",
            end_source="end",
            step_source="task",
            step_name_field="task_name",
            duration_field="dur",
            status_field="result",
            total_duration_field="elapsed",
        )
        assert s["status"] == "ok"
        assert s["total_duration_s"] == 8
        assert s["steps"] == {"ingest": 8}

    def test_summarize_over_grouped_timeline(self) -> None:
        timeline = _job_timeline("job_001") + _job_timeline("job_002", status="failed")
        jobs = group_by(timeline, by="job_id")

        s1 = summarize_job(jobs["job_001"])
        s2 = summarize_job(jobs["job_002"])

        assert s1["status"] == "success"
        assert s2["status"] == "failed"
        assert s1["step_count"] == 2
        assert s2["step_count"] == 2
