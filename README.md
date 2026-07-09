# GCP Observability

Python toolkit for Google Cloud observability — starting with a programmatic Cloud Logging query builder.

## Cloud Logging Query Builder

Build Cloud Logging filter strings in Python instead of writing them by hand.
The output is identical to what you'd type in the **"Build query"** panel of the Cloud Logging console.

### Installation

```bash
pip install -e .
```

### Example — find errors containing "ValueError: Bad"

```python
from datetime import datetime
from gcp_observability.logging import QueryBuilder, Severity, F

query = (
    QueryBuilder()
    .severity_gte(Severity.ERROR)
    .time_range(
        start=datetime(2026, 7, 9, 10, 0, 0),   # 2026-07-09T10:00:00Z
        end=datetime(2026, 7, 9, 10, 30, 0),    # 2026-07-09T10:30:00Z
    )
    .where(
        F("textPayload").has("ValueError: Bad")
        | F("jsonPayload.message").has("ValueError: Bad")
    )
    .build()
)

print(query)
```

Output — paste this directly into the Cloud Logging console or pass to `gcloud`:

```
severity>=ERROR
timestamp>="2026-07-09T10:00:00Z"
timestamp<"2026-07-09T10:30:00Z"
(textPayload:"ValueError: Bad") OR (jsonPayload.message:"ValueError: Bad")
```

### Use with the Cloud Logging client

```python
import google.cloud.logging

client = google.cloud.logging.Client(project="my-gcp-project")
entries = client.list_entries(filter_=query)

for entry in entries:
    print(entry.timestamp, entry.payload)
```

### Use with gcloud CLI

```bash
gcloud logging read 'severity>=ERROR
timestamp>="2026-07-09T10:00:00Z"
timestamp<"2026-07-09T10:30:00Z"
(textPayload:"ValueError: Bad") OR (jsonPayload.message:"ValueError: Bad")'
```

---

## API reference

### `QueryBuilder` methods

| Method | Description |
|---|---|
| `.resource_type(type)` | Filter by resource type (e.g. `cloud_run_revision`) |
| `.resource_label(key, value)` | Filter by a resource label |
| `.project(project_id)` | Restrict to a GCP project |
| `.log_name(name)` | Exact `logName` match |
| `.severity(op, level)` | Severity with explicit operator (`=`, `>=`, etc.) |
| `.severity_eq(level)` | `severity=LEVEL` |
| `.severity_gte(level)` | `severity>=LEVEL` |
| `.severity_range(low, high)` | `severity>=LOW` and `severity<=HIGH` |
| `.time_range(start, end)` | Timestamp window (str or `datetime`, end is exclusive) |
| `.since(hours, minutes, days)` | Relative window from now |
| `.text_payload(value)` | Substring match on `textPayload` |
| `.json_payload(field, op, value)` | Filter on a `jsonPayload` sub-field |
| `.json_payload_has(field, value)` | Substring match on a `jsonPayload` sub-field |
| `.proto_payload(field, op, value)` | Filter on a `protoPayload` sub-field |
| `.http_method(method)` | HTTP request method |
| `.http_status(op, status)` | HTTP response status code |
| `.http_url(value)` | Substring match on request URL |
| `.http_latency_gte(seconds)` | Requests slower than N seconds |
| `.label(key, value)` | Log entry label (special chars in key auto-quoted) |
| `.trace(trace_id)` | Filter by trace ID |
| `.span_id(span_id)` | Filter by span ID |
| `.where(expr)` | Add a raw `F()`-built expression |
| `.raw(filter_str)` | Add a verbatim filter string |
| `.build()` | Return the complete filter string |

### `F()` — low-level expression builder

```python
from gcp_observability.logging import F

# Comparisons
F("severity") >= "ERROR"
F("resource.type") == "cloud_run_revision"
F("httpRequest.status") >= 500

# Substring / has-field
F("textPayload").has("panic")
F("jsonPayload.message").has("timeout")

# Chained dot access
F("resource").labels.zone == "us-central1-a"

# Labels with special characters
F("labels")["k8s-pod/app"] == "my-service"

# Boolean operators
expr_a & expr_b   # AND
expr_a | expr_b   # OR
~expr_a           # NOT
```

### Constants

```python
from gcp_observability.logging import Severity, ResourceType

Severity.DEBUG, Severity.INFO, Severity.NOTICE
Severity.WARNING, Severity.ERROR, Severity.CRITICAL

ResourceType.CLOUD_RUN_REVISION
ResourceType.GKE_CONTAINER
ResourceType.GCE_INSTANCE
ResourceType.LOAD_BALANCER
# ... see constants.py for full list
```
