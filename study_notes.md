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
