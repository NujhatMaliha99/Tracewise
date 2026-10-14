# Live Telemetry

Tracewise supports a bounded OTLP/HTTP **trace** receiver, an environment-scoped service catalog,
and durable incremental incident candidates. It is not a full OpenTelemetry Collector, nor a
gRPC, metric, or OTLP log receiver. No automatic root-cause proof or repair is claimed.

## Local Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-otel-demo.txt
.\.venv\Scripts\python.exe server.py --port 8793 --db storage/live-workspace.sqlite3
```

In the dashboard, register exact `service.name` identities in Service Catalog. Environment comes
from the resource `deployment.environment.name` (legacy `deployment.environment` accepted),
defaulting to `development`. Dependencies reference same-environment services; cycles are rejected
transactionally. Owner and description are metadata, not authorization boundaries. Archived services
reject new spans. Name/environment are immutable. Bind existing monitors from the service editor.

Collection is **disabled by default**. Enable it in Telemetry connections with explicit storage consent.
Disabling stops new telemetry but does not delete retained evidence. Existing `/api/ingest` remains
memory-only and is not silently retained. Monitor/email operations work independently of collection.

## Standard SDK Example

For an isolated local workspace without accounts, this command explicitly registers two example
services, enables retention, and sends official SDK spans from real loopback HTTP requests:

```powershell
.\.venv\Scripts\python.exe examples/otel_demo.py --prepare-workspace --retain
.\.venv\Scripts\python.exe examples/otel_demo.py --failure
```

The second command stops the actual tasks listener before requests. It does not fabricate a successful
downstream server span. W3C trace context connects gateway/client/tasks spans in the healthy run.
Neither command sends email or touches user services. The example defaults to port 8793.

For authenticated deployments, configure `TRACEWISE_OTLP_KEY` with at least 32 random characters
in the server process and exporter process, or supply an operator/admin session. Never commit keys.
The dedicated bearer key authorizes `/v1/traces` only, not catalog edits or evidence reads.
Configure catalog and consent through an admin session. `TRACEWISE_INGEST_KEY` is a different key
for the original transient ingestion endpoint. Local authentication-disabled mode is loopback-only.
The receiver preserves the existing Host/origin checks; public operation still requires accounts,
an exact HTTPS reverse-proxy origin, and proper proxy/TLS configuration. This feature does not deploy it.

OTLP endpoint: `POST /v1/traces`, `application/json` or `application/x-protobuf`, identity or gzip.
Exporters must send Content-Length. JSON uses hexadecimal trace/span IDs and integer enums;
unknown message fields are ignored. Responses use official export/partial-success/status schemas.
Only selected scalar semantic attributes and bounded event names/timestamps are retained; arbitrary
attributes, exception stacks and resource attributes are omitted. Redaction is best-effort, not a
guarantee that sensitive data is absent: do not export secrets or production payloads casually.

## Incident Semantics

- Evidence identity is environment + trace ID + span ID; first evidence wins on retry.
- Failed requests are grouped by deepest observed configured dependency candidate and a fixed
  60-second incident-start window, not a sliding chain. Out-of-order earlier requests may form separate candidates.
- Late spans attach to the original request assignment. Candidate anchors may refine; existing
  incidents are never silently merged. Successful standalone traces do not open incidents.
- Live incidents have owner, status, notes, stable evidence IDs, and an actor-attributed timeline.
- Operator merge/split is version-checked and preserves whole requests. Split must leave at least
  one request in the source. Both manual assignments remain sticky for late evidence. Cross-environment
  merges are forbidden. Merged sources remain in history with a target pointer.
- Bound monitors append observed check evidence while their alert is open and a recovery timeline
  event when it recovers. Recovery does not automatically mark a live incident resolved.
- Request traces and Dependency incidents can select retained live evidence. That view is a
  latest-1,000-record **snapshot** with 30-second grouping; Live incidents is the durable 60-second workflow.

## Limits And Operations

100 services, 300 dependency edges, 2,000 spans per export, 4 MiB compressed and decompressed payload,
10,000 retained spans, 1,000 retained incidents. At capacity new evidence is rejected, not silently
evicted; partial success reports rejected spans. Incident timelines retain the most recent 10,000
events globally and show the latest 200 per incident. Restart preserves settings, evidence and decisions.
No automatic retention expiry/purge, horizontally scaled collector, tenant isolation or production SLO
is provided. Export workspace evidence and use a new isolated database for another bounded evaluation.

Read routes: `/api/catalog`, `/api/telemetry/status`, `/api/telemetry/evidence?environment=development`,
`/api/live-incidents?environment=development`, `/api/live-incidents/:id`, and `/:id/export`.
Admin writes: `/api/catalog`, `/api/catalog/:id` (`changes`, `version`),
`/api/catalog/bind-monitor` (`monitor_id`, `service_id`), `/api/telemetry/settings` (`enabled`, `retain`).
Operator writes: `/api/live-incidents/:id` (`changes`, `version`), `/:id/merge`
(`source_id`, `source_version`, `target_version`), `/:id/split` (`trace_ids`, `version`).

Protocol references: [OTLP specification](https://opentelemetry.io/docs/specs/otlp/)
and [official exporter configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/).
