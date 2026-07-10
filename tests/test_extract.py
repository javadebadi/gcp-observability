"""Tests for gcp_observability.analysis.extract."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from gcp_observability.analysis.extract import JsonExtractor, RegexExtractor, extract
from gcp_observability.logging.client import LogEntry


# ── Helpers ────────────────────────────────────────────────────────────────────

_TS = datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc)


def _entry(
    payload: object,
    payload_type: str = "text",
    severity: str = "INFO",
    insert_id: str = "e1",
) -> LogEntry:
    return LogEntry(
        log_name="projects/test/logs/app",
        severity=severity,
        timestamp=_TS,
        payload=payload,
        payload_type=payload_type,
        resource_type="cloud_run_revision",
        resource_labels={},
        labels={},
        insert_id=insert_id,
    )


def _assert_metadata(record: dict, *, insert_id: str = "e1") -> None:
    """Every extracted record must carry the four standard metadata fields."""
    assert record["_timestamp"] == _TS
    assert record["_severity"] == "INFO"
    assert record["_insert_id"] == insert_id
    assert record["_log_name"] == "projects/test/logs/app"


# ── RegexExtractor — named groups ──────────────────────────────────────────────


class TestRegexNamedGroups:
    def test_basic_match(self) -> None:
        ex = RegexExtractor(r"player (?P<player_id>\d+) level (?P<level>\d+)")
        rec = ex(_entry("player 42 level 7"))
        assert rec is not None
        assert rec["player_id"] == "42"
        assert rec["level"] == "7"

    def test_metadata_always_present(self) -> None:
        ex = RegexExtractor(r"player (?P<player_id>\d+) level (?P<level>\d+)")
        rec = ex(_entry("player 42 level 7"))
        assert rec is not None
        _assert_metadata(rec)

    def test_no_match_returns_none(self) -> None:
        ex = RegexExtractor(r"player (?P<player_id>\d+) level (?P<level>\d+)")
        assert ex(_entry("unrelated log line")) is None

    def test_partial_line_still_matches(self) -> None:
        ex = RegexExtractor(r"error (?P<code>\d+)")
        rec = ex(_entry("2026-07-09 [ERROR] error 404 not found"))
        assert rec is not None
        assert rec["code"] == "404"

    def test_case_insensitive_flag(self) -> None:
        ex = RegexExtractor(r"timeout", flags=re.IGNORECASE)
        assert ex(_entry("TIMEOUT occurred")) is not None
        assert ex(_entry("Timeout exceeded")) is not None

    def test_multiline_payload(self) -> None:
        ex = RegexExtractor(r"player (?P<player_id>\d+)")
        text = "line one\nplayer 99 did something\nline three"
        rec = ex(_entry(text))
        assert rec is not None
        assert rec["player_id"] == "99"


# ── RegexExtractor — positional groups ────────────────────────────────────────


class TestRegexPositionalGroups:
    def test_basic_positional(self) -> None:
        ex = RegexExtractor(r"player (\d+) level (\d+)", fields=["pid", "lvl"])
        rec = ex(_entry("player 5 level 10"))
        assert rec is not None
        assert rec["pid"] == "5"
        assert rec["lvl"] == "10"

    def test_metadata_present(self) -> None:
        ex = RegexExtractor(r"player (\d+)", fields=["pid"])
        rec = ex(_entry("player 7 logged in"))
        assert rec is not None
        _assert_metadata(rec)

    def test_no_match(self) -> None:
        ex = RegexExtractor(r"player (\d+)", fields=["pid"])
        assert ex(_entry("admin logged in")) is None

    def test_fewer_fields_than_groups_truncates(self) -> None:
        # zip() stops at the shortest — extra groups are silently dropped
        ex = RegexExtractor(r"(\d+) (\d+) (\d+)", fields=["a", "b"])
        rec = ex(_entry("1 2 3"))
        assert rec is not None
        assert rec == {**rec, "a": "1", "b": "2"}
        assert "c" not in rec


# ── RegexExtractor — JSON payload ─────────────────────────────────────────────


class TestRegexJsonPayload:
    def test_targets_json_field(self) -> None:
        ex = RegexExtractor(r"error (?P<code>\d+)", json_field="details.message")
        payload = {"details": {"message": "error 503 upstream"}}
        rec = ex(_entry(payload, payload_type="json"))
        assert rec is not None
        assert rec["code"] == "503"

    def test_missing_json_field_returns_none(self) -> None:
        ex = RegexExtractor(r"error (?P<code>\d+)", json_field="details.message")
        rec = ex(_entry({"other": "stuff"}, payload_type="json"))
        assert rec is None

    def test_non_json_entry_with_json_field_returns_none(self) -> None:
        ex = RegexExtractor(r"error (?P<code>\d+)", json_field="message")
        assert ex(_entry("error 500 plain text")) is None

    def test_fallback_to_message_key_in_json(self) -> None:
        # Without json_field, JSON payloads fall back to payload["message"]
        ex = RegexExtractor(r"error (?P<code>\d+)")
        payload = {"message": "error 500 from upstream"}
        rec = ex(_entry(payload, payload_type="json"))
        assert rec is not None
        assert rec["code"] == "500"

    def test_fallback_to_msg_key_in_json(self) -> None:
        ex = RegexExtractor(r"error (?P<code>\d+)")
        payload = {"msg": "error 404 not found"}
        rec = ex(_entry(payload, payload_type="json"))
        assert rec is not None
        assert rec["code"] == "404"

    def test_no_message_key_falls_back_to_str_repr(self) -> None:
        ex = RegexExtractor(r"'status': '(?P<status>\w+)'")
        payload = {"status": "ok"}
        rec = ex(_entry(payload, payload_type="json"))
        assert rec is not None
        assert rec["status"] == "ok"


# ── RegexExtractor — .extract() convenience ───────────────────────────────────


class TestRegexExtractMethod:
    def test_returns_only_matches(self) -> None:
        ex = RegexExtractor(r"player (?P<pid>\d+)")
        entries = [
            _entry("player 1 logged in", insert_id="e1"),
            _entry("admin event", insert_id="e2"),
            _entry("player 2 logged out", insert_id="e3"),
        ]
        records = ex.extract(entries)
        assert len(records) == 2
        assert records[0]["pid"] == "1"
        assert records[1]["pid"] == "2"

    def test_empty_entries(self) -> None:
        ex = RegexExtractor(r"player (?P<pid>\d+)")
        assert ex.extract([]) == []

    def test_no_matches_returns_empty(self) -> None:
        ex = RegexExtractor(r"player (?P<pid>\d+)")
        entries = [_entry("nothing here"), _entry("also nothing")]
        assert ex.extract(entries) == []


# ── JsonExtractor ──────────────────────────────────────────────────────────────


class TestJsonExtractor:
    def test_basic_field_pick(self) -> None:
        ex = JsonExtractor({"user": "userId"})
        rec = ex(_entry({"userId": "u42"}, payload_type="json"))
        assert rec is not None
        assert rec["user"] == "u42"

    def test_dot_path_navigation(self) -> None:
        ex = JsonExtractor({"user": "context.userId", "status": "response.code"})
        payload = {"context": {"userId": "u99"}, "response": {"code": 200}}
        rec = ex(_entry(payload, payload_type="json"))
        assert rec is not None
        assert rec["user"] == "u99"
        assert rec["status"] == "200"

    def test_metadata_always_present(self) -> None:
        ex = JsonExtractor({"x": "val"})
        rec = ex(_entry({"val": "1"}, payload_type="json"))
        assert rec is not None
        _assert_metadata(rec)

    def test_non_json_payload_returns_none(self) -> None:
        ex = JsonExtractor({"x": "val"})
        assert ex(_entry("plain text")) is None

    def test_missing_field_omitted_by_default(self) -> None:
        ex = JsonExtractor({"a": "x", "b": "y"})
        rec = ex(_entry({"x": "1"}, payload_type="json"))  # "y" is absent
        assert rec is not None
        assert rec["a"] == "1"
        assert "b" not in rec

    def test_missing_field_skips_when_require_all(self) -> None:
        ex = JsonExtractor({"a": "x", "b": "y"}, require_all=True)
        assert ex(_entry({"x": "1"}, payload_type="json")) is None

    def test_require_all_passes_when_all_present(self) -> None:
        ex = JsonExtractor({"a": "x", "b": "y"}, require_all=True)
        rec = ex(_entry({"x": "1", "y": "2"}, payload_type="json"))
        assert rec is not None
        assert rec["a"] == "1"
        assert rec["b"] == "2"

    def test_all_fields_missing_returns_none(self) -> None:
        ex = JsonExtractor({"a": "x", "b": "y"})
        assert ex(_entry({"z": "3"}, payload_type="json")) is None

    def test_deeply_nested_path(self) -> None:
        ex = JsonExtractor({"val": "a.b.c"})
        rec = ex(_entry({"a": {"b": {"c": "deep"}}}, payload_type="json"))
        assert rec is not None
        assert rec["val"] == "deep"

    def test_path_broken_midway_returns_none_for_field(self) -> None:
        ex = JsonExtractor({"val": "a.b.c"})
        rec = ex(_entry({"a": "not-a-dict"}, payload_type="json"))
        # "val" missing, no other fields — record is None
        assert rec is None


# ── JsonExtractor — .extract() convenience ────────────────────────────────────


class TestJsonExtractMethod:
    def test_filters_non_json(self) -> None:
        ex = JsonExtractor({"x": "val"})
        entries = [
            _entry({"val": "1"}, payload_type="json", insert_id="e1"),
            _entry("plain text", insert_id="e2"),
            _entry({"val": "3"}, payload_type="json", insert_id="e3"),
        ]
        records = ex.extract(entries)
        assert len(records) == 2
        assert records[0]["x"] == "1"
        assert records[1]["x"] == "3"


# ── Top-level extract() ────────────────────────────────────────────────────────


class TestExtractFunction:
    def test_applies_callable_extractor(self) -> None:
        def my_extractor(entry: LogEntry) -> dict | None:
            if entry.severity == "ERROR":
                return {"msg": entry.payload}
            return None

        entries = [
            _entry("boom", severity="ERROR", insert_id="e1"),
            _entry("ok", severity="INFO", insert_id="e2"),
            _entry("also boom", severity="ERROR", insert_id="e3"),
        ]
        records = extract(entries, my_extractor)
        assert len(records) == 2
        assert records[0]["msg"] == "boom"
        assert records[1]["msg"] == "also boom"

    def test_empty_input(self) -> None:
        assert extract([], lambda e: {"x": 1}) == []

    def test_all_none_returns_empty(self) -> None:
        entries = [_entry("a"), _entry("b")]
        assert extract(entries, lambda e: None) == []

    def test_all_match_returns_all(self) -> None:
        entries = [_entry("a", insert_id="e1"), _entry("b", insert_id="e2")]
        records = extract(entries, lambda e: {"pay": e.payload})
        assert len(records) == 2


# ── Promo key end-to-end ───────────────────────────────────────────────────────


class TestPromoKeyPattern:
    """Mirrors the real use-case from examples/promo_key_tracker.py."""

    _EX = RegexExtractor(
        r"player (?P<player_id>\d+) increased level of heart to (?P<level>\d+)"
        r" with promo key"
    )

    def test_matches_full_message(self) -> None:
        msg = "player 2342342 increased level of heart to 234 with promo key instead of paying"
        rec = self._EX(_entry(msg))
        assert rec is not None
        assert rec["player_id"] == "2342342"
        assert rec["level"] == "234"

    def test_skips_unrelated_entry(self) -> None:
        assert self._EX(_entry("player 1 logged in")) is None

    def test_multiple_players(self) -> None:
        entries = [
            _entry(
                f"player {pid} increased level of heart to {lvl} with promo key",
                insert_id=f"e{i}",
            )
            for i, (pid, lvl) in enumerate([(1, 10), (2, 20), (3, 30)])
        ]
        records = self._EX.extract(entries)
        assert len(records) == 3
        assert [r["player_id"] for r in records] == ["1", "2", "3"]
        assert [int(r["level"]) for r in records] == [10, 20, 30]

    def test_metadata_in_result(self) -> None:
        msg = "player 1 increased level of heart to 5 with promo key"
        rec = self._EX(_entry(msg))
        assert rec is not None
        assert rec["_timestamp"] == _TS
        assert rec["_severity"] == "INFO"
