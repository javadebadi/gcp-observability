# GCP Observability

A Python toolkit for Google Cloud Logging — query builder, fetch client, local
storage, and incremental sync.

**The core idea:** Cloud Logging charges per GB read. This library lets you sync
logs into a local SQLite store once, then query them as many times as you want
at no cost.

```
Cloud Logging API  →  Syncer  →  SQLiteStore  →  your queries (free)
                          ↑
                   tracks watermark:
                   "last synced at 2026-07-09T10:30:00Z"
                   next sync only fetches new logs
```

---

## Installation

```bash
pip install gcp-observability
```

**Authentication** — the library uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).
Run this once before using:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

---

## Quick start

```python
from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer, Severity

# 1. Build a query
query = (
    QueryBuilder()
    .severity_gte(Severity.ERROR)
    .since(hours=24)
)

# 2. Sync logs into local storage (call this on a schedule)
client = Client()
store  = SQLiteStore("logs.db")
syncer = Syncer(client, store)

result = syncer.sync(query, project="my-gcp-project", sync_id="prod-errors")
print(result)
# [prod-errors] fetched=42 stored=40 duplicates=2 window=... → ...

# 3. Query locally — no Cloud Logging charges
entries = store.query(severity_gte="ERROR", search="ValueError", limit=50)
for entry in entries:
    print(entry.timestamp, entry.severity, entry.payload)
```

---

## Components

### QueryBuilder

Builds [Cloud Logging filter strings](https://cloud.google.com/logging/docs/view/logging-query-language)
programmatically. The output is identical to what you'd type in the Cloud
Logging console.

```python
from gcp_observability import QueryBuilder, Severity, ResourceType, F
from datetime import datetime

query = (
    QueryBuilder()
    .resource_type(ResourceType.CLOUD_RUN_REVISION)
    .resource_label("service_name", "my-api")
    .severity_gte(Severity.ERROR)
    .time_range(
        start=datetime(2026, 7, 9, 10, 0, 0),
        end=datetime(2026, 7, 9, 10, 30, 0),
    )
    .json_payload_has("message", "timeout")
)

print(query.build())
# resource.type=cloud_run_revision
# resource.labels."service_name"="my-api"
# severity>=ERROR
# timestamp>="2026-07-09T10:00:00Z"
# timestamp<"2026-07-09T10:30:00Z"
# jsonPayload.message:timeout
```

#### Severity

```python
QueryBuilder().severity_gte(Severity.ERROR)    # ERROR and above
QueryBuilder().severity_eq(Severity.WARNING)   # WARNING only
QueryBuilder().severity_range("WARNING", "ERROR")  # WARNING and ERROR

# Severity levels (low → high):
# DEFAULT  DEBUG  INFO  NOTICE  WARNING  ERROR  CRITICAL  ALERT  EMERGENCY
```

#### Timestamp

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Relative window
QueryBuilder().since(hours=1)
QueryBuilder().since(minutes=30)
QueryBuilder().since(days=7)

# Absolute window — accepts str or datetime (naïve assumed UTC)
QueryBuilder().time_range("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z")
QueryBuilder().time_range(datetime(2026, 7, 1), datetime(2026, 7, 2))

# Timezone-aware datetime — converted to UTC automatically
QueryBuilder().time_range(datetime(2026, 7, 1, tzinfo=ZoneInfo("Asia/Tehran")))
```

#### Payload

```python
# textPayload substring search
QueryBuilder().text_payload("ValueError: Bad")

# jsonPayload field comparison
QueryBuilder().json_payload("statusCode", ">=", 500)
QueryBuilder().json_payload_has("message", "timeout")

# protoPayload (Cloud Audit Logs)
QueryBuilder().proto_payload("methodName", "=", "SetIamPolicy")

# Search across ALL fields at once
QueryBuilder().global_search("ValueError: Bad")
```

#### HTTP requests

```python
QueryBuilder().http_method("GET")
QueryBuilder().http_status(">=", 500)
QueryBuilder().http_url("/api/v1/users")
QueryBuilder().http_latency_gte(2.5)   # requests slower than 2.5 seconds
```

#### Resource, project, labels

```python
QueryBuilder().resource_type(ResourceType.CLOUD_RUN_REVISION)
QueryBuilder().resource_label("cluster_name", "prod")
QueryBuilder().project("my-project")                # all logs in project
QueryBuilder().project("my-project", log_id="app")  # specific log stream
QueryBuilder().label("k8s-pod/app", "my-service")   # entry labels
```

#### Trace and span

```python
QueryBuilder().trace("abc123def")
QueryBuilder().span_id("span456")
QueryBuilder().sampled(True)
```

#### Low-level `F()` — compose arbitrary expressions

```python
from gcp_observability import F

# Single field
F("severity") >= "ERROR"
F("resource.type") == "cloud_run_revision"
F("httpRequest.status") >= 500
F("textPayload").has("panic")
F("resource").labels.zone == "us-central1-a"
F("labels")["k8s-pod/app"] == "my-service"  # special-char key

# Boolean composition
expr = (
    (F("resource.type") == "cloud_run_revision")
    & ((F("severity") >= "ERROR") | (F("jsonPayload.level") == "fatal"))
    & ~F("textPayload").has("healthcheck")
)
QueryBuilder().where(expr).build()

# Raw filter passthrough
QueryBuilder().raw('jsonPayload.message=~".*panic.*"')
```

#### `ResourceType` constants

```python
ResourceType.CLOUD_RUN_REVISION   ResourceType.GKE_CONTAINER
ResourceType.GCE_INSTANCE         ResourceType.LOAD_BALANCER
ResourceType.CLOUD_FUNCTION       ResourceType.APP_ENGINE
ResourceType.CLOUD_SQL            ResourceType.PUBSUB_TOPIC
ResourceType.BIG_QUERY            ResourceType.STORAGE
# see constants.py for the full list
```

---

### Client

Fetches log entries from Cloud Logging. One instance works across all your
projects — credentials live at the client level, projects are specified per
call.

```python
from gcp_observability import Client, QueryBuilder, Severity

client = Client()                        # ADC, no default project
client = Client(project="my-project")   # with default project

query = QueryBuilder().severity_gte(Severity.ERROR).since(hours=1)

# fetch() — returns a list
entries = client.fetch(query, project="my-project")

# iter() — streams one entry at a time (memory-efficient for large sets)
for entry in client.iter(query, project="my-project"):
    print(entry.timestamp, entry.payload)

# Multi-project in one call
entries = client.fetch(
    query,
    projects=["project-a", "project-b", "project-c"],
    max_results=500,
)

# Control ordering and page size
entries = client.fetch(query, project="my-project", order_by="timestamp asc")
```

#### `LogEntry` fields

| Field | Type | Description |
|---|---|---|
| `timestamp` | `datetime` | Entry timestamp (UTC-aware) |
| `severity` | `str` | e.g. `"ERROR"`, `"WARNING"` |
| `payload` | `str \| dict` | textPayload or jsonPayload |
| `payload_type` | `str` | `"text"`, `"json"`, or `"proto"` |
| `log_name` | `str` | Full log name |
| `project` | `str` | Project ID (extracted from log_name) |
| `log_id` | `str` | Log ID (extracted from log_name) |
| `resource_type` | `str` | e.g. `"cloud_run_revision"` |
| `resource_labels` | `dict` | Resource label key-value pairs |
| `labels` | `dict` | Entry labels |
| `insert_id` | `str` | Unique entry ID |
| `trace` | `str \| None` | Trace ID |
| `span_id` | `str \| None` | Span ID |
| `http_request` | `dict \| None` | HTTP request details |

---

### SQLiteStore

Local store backed by SQLite. All writes are idempotent — re-syncing the same
logs never creates duplicates.

```python
from gcp_observability import SQLiteStore

store = SQLiteStore("logs.db")     # persists to disk
store = SQLiteStore(":memory:")    # in-memory for testing
```

#### Querying local data

```python
# All filters are optional and combinable
entries = store.query(
    project="my-project",
    log_id="cloudrun.googleapis.com/requests",
    resource_type="cloud_run_revision",
    severity_gte="WARNING",
    start=datetime(2026, 7, 1, tzinfo=timezone.utc),
    end=datetime(2026, 7, 2, tzinfo=timezone.utc),
    search="ValueError",           # substring search across payload
    limit=1000,
    order="desc",                  # "desc" (newest first) or "asc"
)

# Total entries in store
print(store.count())
```

#### Sync state

```python
# See all sync jobs and their watermarks
for job in store.list_sync_jobs():
    print(job["sync_id"], job["last_synced_at"], job["total_entries_synced"])

# Read a specific watermark
watermark = store.get_watermark("prod-errors")
# datetime(2026, 7, 9, 10, 30, 0, tzinfo=timezone.utc)
```

---

### Syncer

Incremental sync engine. Each call fetches only new logs (from the last
watermark to now), stores them, and advances the watermark — all in one
atomic transaction.

```python
from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer, Severity

client = Client()
store  = SQLiteStore("logs.db")
syncer = Syncer(client, store)
```

#### Regular sync — run on a schedule

```python
result = syncer.sync(
    QueryBuilder().severity_gte(Severity.WARNING),
    project="my-project",
    sync_id="prod-warnings",      # name for this sync job
)
print(result)
# [prod-warnings] fetched=12 stored=10 duplicates=2 window=... → ...
```

On the first run with no watermark, it fetches the last 24 hours by default.
You can change this:

```python
syncer = Syncer(client, store, default_lookback=timedelta(hours=6))
```

#### Backfill — fetch historical data in chunks

```python
from datetime import datetime, timezone

results = syncer.backfill(
    QueryBuilder().severity_gte(Severity.ERROR),
    project="my-project",
    sync_id="prod-errors-backfill",
    start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    end=datetime(2026, 7, 1, tzinfo=timezone.utc),
    window_hours=6,               # fetch 6 hours at a time
)

total_stored = sum(r.stored for r in results)
print(f"Backfilled {total_stored} entries across {len(results)} windows")
```

#### Multi-project sync

```python
syncer.sync(
    QueryBuilder().severity_gte(Severity.ERROR),
    projects=["project-a", "project-b"],
    sync_id="all-projects-errors",
)
```

---

## Common patterns

### Error monitoring across services

```python
syncer.sync(
    QueryBuilder()
        .severity_gte(Severity.ERROR)
        .resource_type(ResourceType.CLOUD_RUN_REVISION),
    project="my-project",
    sync_id="run-errors",
)

# Find all timeout errors
entries = store.query(severity_gte="ERROR", search="timeout")

# Find errors from a specific service
entries = store.query(
    severity_gte="ERROR",
    resource_type="cloud_run_revision",
    search="payment-api",
)
```

### Find a specific exception

```python
from gcp_observability import F

syncer.sync(
    QueryBuilder()
        .severity_gte(Severity.ERROR)
        .time_range(
            start=datetime(2026, 7, 9, 10, 0, 0),
            end=datetime(2026, 7, 9, 10, 30, 0),
        )
        .where(
            F("textPayload").has("ValueError: Bad")
            | F("jsonPayload.message").has("ValueError: Bad")
        ),
    project="my-project",
    sync_id="valueerror-hunt",
)

entries = store.query(search="ValueError: Bad")
```

### Cloud Audit Logs

```python
syncer.sync(
    QueryBuilder()
        .proto_payload("methodName", "=", "SetIamPolicy")
        .proto_payload("authenticationInfo.principalEmail", ":", "@"),
    project="my-project",
    sync_id="iam-changes",
)
```

### Scheduled sync (cron / Cloud Scheduler)

Put this in a script and run it on a schedule:

```python
#!/usr/bin/env python
from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer, Severity

client = Client()
store  = SQLiteStore("/data/logs.db")
syncer = Syncer(client, store)

for project in ["project-a", "project-b"]:
    result = syncer.sync(
        QueryBuilder().severity_gte(Severity.WARNING),
        project=project,
        sync_id=f"{project}-warnings",
    )
    print(result)
```

---

## Use the filter string directly

If you just want the filter string to use elsewhere (Cloud Logging console,
`gcloud` CLI, or the `google-cloud-logging` client directly):

```python
from gcp_observability import QueryBuilder, Severity, F

query = (
    QueryBuilder()
    .severity_gte(Severity.ERROR)
    .since(hours=24)
    .where(
        F("textPayload").has("ValueError: Bad")
        | F("jsonPayload.message").has("ValueError: Bad")
    )
    .build()
)

print(query)
# severity>=ERROR
# timestamp>="2026-07-09T10:00:00Z"
# (textPayload:"ValueError: Bad") OR (jsonPayload.message:"ValueError: Bad")
```

Paste directly into the Cloud Logging console, or:

```bash
gcloud logging read 'severity>=ERROR
timestamp>="2026-07-09T10:00:00Z"
(textPayload:"ValueError: Bad") OR (jsonPayload.message:"ValueError: Bad")' \
  --project=my-project
```

Or use with the `google-cloud-logging` client:

```python
import google.cloud.logging

gcp_client = google.cloud.logging.Client(project="my-project")
entries = gcp_client.list_entries(filter_=query)
```

---

## Development

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — used for dependency management
- A GCP project with ADC configured (integration tests only)

### Set up

```bash
git clone https://github.com/javadebadi/gcp-observability
cd gcp-observability

# Create venv and install all dependencies (including dev tools)
uv sync --all-groups
```

### Run the unit tests

Unit tests use no GCP credentials and run in milliseconds:

```bash
pytest
```

Expected output:

```
186 passed, 7 deselected in 0.17s
```

The `7 deselected` are the integration tests — excluded by default.

### Run the integration tests

Integration tests write real log entries to GCP, wait for indexing, sync
them back, and assert the full pipeline output. They take ~35 seconds each
and require a GCP project with `logging.logEntries.create` and
`logging.logEntries.list` permissions.

```bash
# Authenticate once
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID

# Run integration tests
GCP_TEST_PROJECT=your-project-id pytest -m integration -v
```

Each integration test uses a UUID-scoped log name so parallel runs and
re-runs never interfere with each other.

### Linting and type checking

```bash
# Check and auto-fix lint issues
ruff check --fix .

# Auto-format
ruff format .

# Type check
ty check gcp_observability/
```

All three must be clean before submitting a PR. The CI equivalent is:

```bash
ruff check . && ty check gcp_observability/ && pytest
```

### Project layout

```
gcp_observability/
├── logging/
│   ├── expressions.py   # expression tree: F(), Comparison, And, Or, Not
│   ├── query.py         # QueryBuilder fluent API
│   ├── constants.py     # Severity, ResourceType
│   └── client.py        # Client, LogEntry
├── storage/
│   └── sqlite.py        # SQLiteStore — local store + watermark tracking
├── sync.py              # Syncer — incremental sync engine
├── analysis/
│   └── extract.py       # RegexExtractor, JsonExtractor, Pipeline, merge()
└── queries/
    └── general.py       # pre-built QueryBuilder presets

examples/
├── promo_key_tracker.py     # RegexExtractor on text logs
└── job_lifecycle_tracker.py # Pipeline with multiple patterns

tests/
├── test_expressions.py
├── test_query.py
├── test_storage.py
├── test_sync.py
├── test_extract.py
└── integration/
    └── test_job_lifecycle.py   # real GCP — requires GCP_TEST_PROJECT
```

### Contributing

1. Fork the repo and create a branch from `master`.
2. Write tests for any new behaviour — check coverage with `pytest -v`.
3. Make sure `ruff check .`, `ty check gcp_observability/`, and `pytest` all
   pass before opening a PR.
4. Keep PRs focused — one feature or fix per PR.
5. Integration tests are welcome but not required for pure-Python changes.

---

## License

MIT — see [LICENSE](LICENSE).
