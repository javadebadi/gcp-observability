# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install all dependencies (including dev tools)
uv sync --all-groups

# Run unit tests (integration tests excluded by default)
pytest

# Run a single test file
pytest tests/test_query.py

# Run a single test by name
pytest tests/test_query.py::test_severity_gte

# Run integration tests (requires GCP credentials and GCP_TEST_PROJECT env var)
GCP_TEST_PROJECT=your-project-id pytest -m integration -v

# Lint + auto-fix
ruff check --fix .

# Format
ruff format .

# Type check
ty check gcp_observability/

# Full CI check (must all pass before any PR)
ruff check . && ty check gcp_observability/ && pytest
```

## Architecture

The library has three layers that compose into a cost-saving pipeline:

```
Cloud Logging API  →  Client  →  Syncer  →  SQLiteStore  →  local queries (free)
```

**Layer 1 — Query building** (`gcp_observability/logging/`)

- `expressions.py` — low-level expression tree. `Field` (aliased as `F()`) overloads comparison operators (`==`, `>=`, `:` via `.has()`) to produce `Comparison`, `And`, `Or`, `Not`, and `Raw` nodes, each with a `.build() -> str` method.
- `query.py` — `QueryBuilder` is a fluent wrapper that accumulates `Expr` nodes and joins them with newlines on `.build()`. Every method returns `self` for chaining. `QueryBuilder` and `F()` are the two primary user-facing APIs for constructing filter strings.
- `constants.py` — `Severity` and `ResourceType` string constants.

**Layer 2 — Fetch and store** (`gcp_observability/logging/client.py`, `gcp_observability/storage/sqlite.py`, `gcp_observability/sync.py`)

- `client.py` — `Client` wraps `google-cloud-logging` and returns `LogEntry` dataclasses. `iter()` streams; `fetch()` collects. The `LogEntry.project` and `LogEntry.log_id` properties parse the `log_name` string (`projects/{project}/logs/{log_id}`).
- `sqlite.py` — `SQLiteStore` persists entries with `insert_id` as PRIMARY KEY so `INSERT OR IGNORE` makes all writes idempotent. Severity is stored as both a string and an integer (`severity_level`) for fast `>=` comparisons. WAL mode is on. The `sync_state` table tracks per-`sync_id` watermarks.
- `sync.py` — `Syncer` orchestrates the sync loop: read watermark → clamp to `(watermark, now]` → fetch → store + advance watermark in one transaction. `backfill()` splits a large historical range into fixed `window_hours` chunks.

**Layer 3 — Analysis** (`gcp_observability/analysis/`)

- `extract.py` — Stage 1. An extractor is any callable `LogEntry -> dict | None`. Built-ins: `RegexExtractor` (named or positional capture groups from text/JSON payloads) and `JsonExtractor` (dot-path field picks from JSON payloads). Every returned dict gets four `_`-prefixed metadata keys: `_timestamp`, `_severity`, `_insert_id`, `_log_name`. `Pipeline` runs multiple named extractors and merges their results into a single `_timestamp`-sorted timeline, stamping each record with `_source`.
- `insights.py` — Stage 2. Pure functions over `list[dict]`: `filter_by`, `group_by`, `count_by`, `top_n`, and `summarize_job` (lifecycle summary for job-oriented pipelines).

## Key design invariants

- `insert_id` is the global dedup key. Missing `insert_id`s (rare) get a synthetic `_no_id_{timestamp}` fallback in `sqlite.py:_entry_to_row`.
- Naïve datetimes are always assumed UTC throughout; `_to_utc` / `_ensure_utc` helpers enforce this at boundaries.
- The `Syncer` never moves the watermark backward — if a run crashes after storing entries but before committing the watermark, the next run re-fetches the same window and `INSERT OR IGNORE` discards duplicates silently.
- `QueryBuilder` filters are joined with newlines (not `AND`) because Cloud Logging treats a bare newline as implicit AND — this matches what the Cloud Logging console generates.
