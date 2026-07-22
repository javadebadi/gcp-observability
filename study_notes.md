# GCP Cloud Logging — Study Notes

## Payload types

Cloud Logging REST API / console exposes three distinct fields per log entry:

| Console field | Meaning |
|---|---|
| `textPayload` | Plain string — unstructured logs |
| `jsonPayload` | Structured JSON object — your app logged a dict |
| `protoPayload` | Protocol Buffer message — emitted by **GCP services themselves** (e.g. Cloud Audit Logs, VPC flow logs). Your app never writes this. |

The **`google-cloud-logging` Python library** collapses these three into a single `entry.payload` attribute. The Python *type* tells you which one it was:

| Python type of `entry.payload` | Original field |
|---|---|
| `str` | textPayload |
| `dict` | jsonPayload |
| `google.protobuf.message.Message` | protoPayload |

This is why `_parse_entry` uses `isinstance` checks — the library doesn't expose `.textPayload` / `.jsonPayload` / `.protoPayload` as separate attributes.

---

## `resource_labels` vs `labels`

Both are `dict[str, str]` on a log entry but they serve different purposes.

**`resource_labels`** — predefined by Google per resource type. Identify *which specific instance* of a resource emitted the log. Keys are fixed and documented.

| resource_type | Example keys |
|---|---|
| `cloud_run_revision` | `service_name`, `revision_name`, `location`, `project_id` |
| `gce_instance` | `instance_id`, `zone`, `project_id` |
| `k8s_container` | `cluster_name`, `namespace_name`, `pod_name`, `container_name` |

**`labels`** — arbitrary key-value metadata on the log *entry* itself. Set by your application, middleware, or GCP services. No predefined schema.

```json
{ "environment": "production", "app_version": "1.4.2", "request_id": "abc-123" }
```

Key distinction:
- `resource_labels` → *where* did this log come from (which machine/service/pod)?
- `labels` → *what extra metadata* did the application want to tag on this entry?

Labels are not redundant even with `jsonPayload` because:
1. They work across all payload types (text, json, proto)
2. Can be injected by infrastructure without touching the application payload
3. Separate operational metadata from business domain data

---

## `trace` and `span_id`

**Trace** — unique ID for the entire end-to-end journey of one request across all services. Same trace ID appears on every log entry across every service that handled that request.

**Span** — a single unit of work *within* a trace. One trace is made of many spans:

```
trace: abc123
  ├── span: 1a2b  → LB receives request
  ├── span: 3c4d  → Service A processes
  │     └── span: 5e6f  → Service A calls Service B
  │           └── span: 7g8h  → DB query inside Service B
```

A log entry carries both so you know:
- **which request** it belongs to (`trace`)
- **which specific operation** within that request was running when it was logged (`span_id`)

### Who creates them?

| Actor | What they do |
|---|---|
| GCP (Cloud Run, App Engine, GKE) | Automatically creates trace + span at the HTTP request level |
| Your app via OpenTelemetry | Creates spans for individual operations inside your service (functions, DB calls, etc.) |

### Why `QueryBuilder.trace()` has `exact=False` but `span_id()` doesn't

`trace` is stored as a full path: `projects/{project}/traces/{hex_id}`. When you only have the short hex ID, you need substring match (`:`) to find it. Default is `exact=False`.

`span_id` is always a fixed 16-character hex string with no path prefix — partial match has no practical use, so only exact match is supported.

---

## OpenTelemetry on GCP

OpenTelemetry **creates** the trace ID at the first service in the chain. Every downstream service **uses** (propagates) that same ID.

**Problem:** GCP's built-in tracing uses `X-Cloud-Trace-Context` header; OTel uses W3C `traceparent` header. Without bridging them, you get two separate trace IDs for the same request.

**Solution:** Add `opentelemetry-propagator-gcp` — it bridges the two systems so they share one trace ID.

```python
from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
propagators.set_global_textmap(CloudTraceFormatPropagator())
```

Rule: **always add the GCP propagator when using OTel on GCP**, otherwise Cloud Trace and log entries will have mismatched trace IDs.

---

## Severity levels

Cloud Logging defines 9 named severity levels, each mapped to a numeric value:

| Name | Integer | Meaning |
|---|---|---|
| `DEFAULT` | 0 | No severity assigned |
| `DEBUG` | 100 | Detailed debug info |
| `INFO` | 200 | Routine operational messages |
| `NOTICE` | 300 | Normal but significant events |
| `WARNING` | 400 | Something unexpected, not an error |
| `ERROR` | 500 | An error occurred |
| `CRITICAL` | 600 | Severe error, some functionality lost |
| `ALERT` | 700 | Action must be taken immediately |
| `EMERGENCY` | 800 | System is unusable |

### Key distinctions

**`DEFAULT` ≠ `DEBUG`** — `DEFAULT` (0) means the entry was written *without specifying any severity*. It sits below DEBUG in the ordering. Many application loggers emit `DEFAULT` if you don't explicitly set a level.

**Gaps are intentional** — Cloud Logging accepts any integer as a severity value, not just the named ones. The named levels use multiples of 100 so there is room for custom levels between them (e.g. 150, 250).

### Why severity is stored as both string and integer

String comparison gives wrong answers for range queries:

```python
"ERROR" >= "WARNING"  # False — alphabetically E < W, but ERROR > WARNING numerically
```

`sqlite.py` stores `severity_level` as an integer and indexes it so that queries like `severity_level >= 400` (WARNING and above) are fast and correct. The string is kept alongside it for display.

---

## Writing logs in a GCP app — best practices

### Three approaches

| Approach | How | When to use |
|---|---|---|
| `StructuredLogHandler` | JSON to stdout, no API calls | Large apps, production (recommended) |
| `CloudLoggingHandler` | Direct API call per log line | Small scripts, writing from outside GCP |
| Raw `print(json.dumps(...))` | Manual JSON to stdout | Minimal dependencies, one-off tools |

### Why `StructuredLogHandler` is the production default

`CloudLoggingHandler` makes a **synchronous network call** for every `log.warning()`. Under load this adds latency to your request path and can cascade if Cloud Logging is slow. `StructuredLogHandler` writes JSON to stdout — GCP captures it automatically on Cloud Run / GKE with zero network overhead.

```python
import logging
from google.cloud.logging.handlers import StructuredLogHandler

logging.basicConfig(handlers=[StructuredLogHandler()], level=logging.INFO)
log = logging.getLogger(__name__)

log.warning("disk nearly full", extra={"json_fields": {"disk_pct": 92}})
```

### The flow

```
log.warning(...)
    → StructuredLogHandler formats as JSON (severity, trace, labels)
    → stdout
    → GCP log capture (automatic on Cloud Run / GKE)
    → Cloud Logging API
```

### What large apps add on top

- **One logger per module**: `log = logging.getLogger(__name__)` — gives fine-grained level control per package.
- **Structured `json_fields`**: add queryable fields like `user_id`, `amount`, `job_id` so you can filter and build log-based metrics without changing app code.
- **Trace correlation**: `StructuredLogHandler` + OpenTelemetry injects `logging.googleapis.com/trace` automatically so logs link to Cloud Trace spans.

### Severity mapping: Python → GCP

The `google-cloud-logging` library translates Python level names to GCP severity names automatically. The integers are on completely different scales but the names match up:

| Python level | Python int | GCP severity | GCP int |
|---|---|---|---|
| `DEBUG` | 10 | `DEBUG` | 100 |
| `INFO` | 20 | `INFO` | 200 |
| `WARNING` | 30 | `WARNING` | 400 |
| `ERROR` | 40 | `ERROR` | 500 |
| `CRITICAL` | 50 | `CRITICAL` | 600 |

`NOTICE`, `ALERT`, and `EMERGENCY` have no Python stdlib equivalent — you'd have to write to the GCP API directly to use them.

---

## Log filter language

The filter language is accepted everywhere Cloud Logging takes a filter — the console, the API's `filter` parameter, log sinks, and log-based alerts. It is also exactly what `QueryBuilder.build()` outputs.

### Operators

| Operator | Name | Example | Meaning |
|---|---|---|---|
| `=` | exact | `severity="ERROR"` | field equals value exactly |
| `!=` | not equal | `severity!="DEBUG"` | field does not equal value |
| `>` `>=` `<` `<=` | comparison | `severity>=WARNING` | ordered comparison |
| `:` | has / substring | `textPayload:"timeout"` | field contains this string |
| `=~` | regex match | `textPayload=~"error.*disk"` | matches RE2 regex |
| `!~` | regex not-match | `textPayload!~"healthcheck"` | does not match regex |

The `:` operator has nuance:
- On a **string field**: true if the value *contains* the substring (case-insensitive)
- On a **message / repeated field**: true if the field exists and holds the value ("has" check)
- `field:*` — field has any value at all (existence check)

### Combining expressions

| Syntax | Meaning |
|---|---|
| `expr1\nexpr2` (newline) | implicit AND |
| `expr1 expr2` (space) | also implicit AND |
| `expr1 AND expr2` | explicit AND |
| `expr1 OR expr2` | OR |
| `NOT expr` | negation |
| `(expr1 OR expr2) AND expr3` | grouping |

**Why `QueryBuilder` uses newlines:** The Cloud Logging console generates filters with newlines between terms. The library matches this so filters copy-paste between the console and the library without modification.

### Field paths

Fields are accessed with dot notation:

```
# Top-level
severity >= WARNING
resource.type = "cloud_run_revision"
logName : "projects/my-proj/logs/my-app"

# Into jsonPayload
jsonPayload.user_id = "abc-123"
jsonPayload.amount > 1000

# Into labels
labels.environment = "production"

# Into resource labels
resource.labels.service_name = "api"
```

### Severity comparison in filter language vs SQLite

In the Cloud Logging filter language, GCP knows the severity ordering by name:

```
severity >= WARNING   ← valid; GCP resolves WARNING → 400 internally
```

This works because the filter language has built-in knowledge of severity ordering. In SQLite, string comparison gives wrong results (`'ERROR' >= 'WARNING'` is `False` alphabetically), which is why `SQLiteStore` stores `severity_level` as an integer.

### Important built-in fields

| Field | Notes |
|---|---|
| `logName` | Full path: `projects/{p}/logs/{log_id}` |
| `resource.type` | e.g. `"cloud_run_revision"`, `"gce_instance"` |
| `resource.labels.*` | Per resource type |
| `severity` | Compared by name using GCP's numeric ordering |
| `timestamp` | RFC 3339: `"2024-01-15T10:00:00Z"` |
| `insertId` | Dedup key |
| `trace` | Full path: `projects/{p}/traces/{hex}` |
| `spanId` | 16-char hex |
| `labels.*` | Application/infrastructure labels |
| `textPayload` | Unstructured log text |
| `jsonPayload.*` | Structured JSON fields |

### How `expressions.py` maps to the filter language

`F()` / `Field` overloads Python operators to produce filter strings:

```python
F("severity") >= "WARNING"       # → severity >= "WARNING"
F("jsonPayload.user_id") == "x"  # → jsonPayload.user_id = "x"
F("textPayload").has("timeout")  # → textPayload:"timeout"
```

`.has()` exists because Python has no `:` operator to overload — it mirrors the Cloud Logging `:` semantics directly.

---

## Log-based metrics & alerting

These are the bridge between Cloud **Logging** and Cloud **Monitoring** — they turn a stream of log entries into a time-series metric you can graph, threshold, and alert on.

### Two metric types

| Type | What it tracks | Example |
|---|---|---|
| **Counter** | Number of log entries matching a filter, per time interval | Count of `severity>=ERROR` entries per minute |
| **Distribution** | A numeric value pulled out of each matching entry, bucketed into a histogram | Request latency from `jsonPayload.latency_ms` |

### System vs user-defined

- **System-defined metrics** — GCP ships these for free, e.g. `logging.googleapis.com/byte_count`. You don't create them.
- **User-defined metrics** — you define a filter (same filter language as `QueryBuilder`), and optionally:
  - **Label extractors**: regex on a field (e.g. `resource.labels.service_name`) pulled into a metric label — up to 10 labels per metric, so you can slice the metric by dimension in Monitoring.
  - **Value extractor** (distribution only): which numeric field to bucket, plus a unit.

### Metrics see logs sinks don't

Log-based metrics are evaluated against the **full ingested log stream**, independent of the `_Default` sink's inclusion/exclusion filters. You can write an exclusion filter to stop *storing* a noisy log (saving storage cost) while a log-based metric still *counts* every occurrence. This is the standard pattern for high-volume logs you want to measure but not keep.

The flip side: metrics are **not retroactive** — a metric only counts entries ingested after it was created; it cannot be backfilled from log history.

### Alerting on top

A log-based metric becomes a normal Cloud Monitoring metric (`logging.googleapis.com/user/<metric-name>`). From there it's identical to any other Monitoring metric:

1. Create an **alerting policy** with a condition on the metric (threshold, rate-of-change, absence).
2. Attach **notification channels** (email, Slack, PagerDuty, Pub/Sub, etc.).
3. Cloud Monitoring evaluates the condition continuously and fires when breached.

This is why you'd reach for a counter metric like `severity>=ERROR AND resource.type="cloud_run_revision"` — not to browse logs, but to page someone when the error rate spikes.

### Sinks vs log-based metrics

| | Log sink | Log-based metric |
|---|---|---|
| Purpose | Export/store raw log entries | Aggregate into a number/histogram |
| Destination | BigQuery, Pub/Sub, Cloud Storage, another bucket | Cloud Monitoring time series |
| Use case | Analysis, long-term retention, compliance | Dashboards, alerting, SLOs |
| Retroactive | N/A (routes future logs) | No — counts from creation forward |
