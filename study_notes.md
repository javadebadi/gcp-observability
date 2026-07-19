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
