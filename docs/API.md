# Workspace REST API (v0.5)

Multi-service endpoints (see OBSERVABILITY.md for protocol and boundaries):

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/traces/analyze` | Analyze `{content}` transiently; viewer permitted |
| GET | `/api/experiments` | Latest 50 persisted experiment metadata rows |
| POST | `/api/experiments` | Admin start `{config:{seeds,scenarios},retain:true}` |
| POST | `/api/experiments/cancel` | Admin request cooperative worker cancellation |
| GET | `/api/experiments/:id` | Full measured samples, spans and correlation results |
| GET | `/api/experiments/:id/export` | JSON artifact attachment |
| GET | `/api/experiments/:id/report` | Markdown measured report attachment |
| POST | `/api/experiments/:id/cases` | Admin save `{run_id,episode_id,retain:true}` as an idempotent synthetic case |

Seeds: 1-5 unique integers 0-999999. Scenarios: distinct selections from baseline,
payments-timeout, service-down, database-contention. One worker per process;
concurrent start returns 409. No external target URLs or notification destinations
are accepted by experiment endpoints.

Admin operations now include notification `channel` (`none`, `webhook`, or
`email`). Delivery `sent` means remote HTTP/SMTP acceptance, not guaranteed email
inbox delivery. SMTP settings and addresses are never returned to the dashboard.
See [email setup](EMAIL.md) for environment settings and the local test workflow.

Default origin: `http://127.0.0.1:8790`. Optional shared-workspace role-based sessions.
JSON mutation endpoints require an approved Host, same origin when supplied, and
`Content-Type: application/json`. Errors use `{ "error": "message" }`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Version and process health |
| GET | `/api/session` | Authentication enabled flag and current identity, or null |
| POST | `/api/login` | `{username,password}`; HttpOnly session cookie |
| POST | `/api/logout` | Revoke current session and clear cookie |
| GET | `/api/operations` | Admin-only egress hosts, delivery status and mutation audit |
| POST | `/api/operations` | Admin-only `{action:"retry",event_key}` for failed delivery |
| GET | `/api/capabilities` | Dependency and storage availability |
| GET | `/api/sample` | Synthetic JSONL attachment |
| POST | `/api/sample` | Analyze the synthetic sample |
| POST | `/api/analyze` | Analyze `{content, filename}` without retention |
| POST | `/api/demo` | Execute 15 controlled loopback HTTP requests |
| POST | `/api/ingest` | Parse/redact `{content}` into the rolling memory buffer |
| GET | `/api/live` | Buffered records and latest-100-event threshold state |
| GET | `/api/monitors` | Configurations, current states and worker fault |
| POST | `/api/monitors` | Create `{config, retain: true}` |
| GET | `/api/monitors/:id` | Settings, state and latest 200 checks |
| POST | `/api/monitors/:id` | Update `{changes}`; target URL immutable |
| POST | `/api/monitors/:id/check` | Perform a manual probe, including when paused |
| GET | `/api/alerts` | Latest 200 monitor alerts with linked case IDs |
| POST | `/api/alerts/:id/acknowledge` | Acknowledge, not recover |
| GET/POST | `/api/monitor-demo` | Read/set `{mode: healthy or failure}` for synthetic demo |
| GET | `/monitor-demo` | Controlled health target, HTTP 200 or injected 503 |
| POST | `/api/live/clear` | Clear transient buffer, not saved cases |
| GET | `/api/cases` | Metadata for non-archived saved cases |
| GET | `/api/cases?archived=1` | Include archived cases |
| POST | `/api/cases` | Save `{content, filename, title, priority, source}` |
| GET | `/api/cases/:id` | Saved report, metadata, notes and audit events |
| POST | `/api/cases/:id` | Update `{version, changes}` |
| POST | `/api/cases/:id/notes` | Add redacted `{body}` note |
| GET | `/api/cases/:id/export` | JSON evidence attachment |
| GET | `/api/cases/:id/report` | Markdown incident-report attachment |
| GET | `/api/analytics` | Descriptive saved-case aggregates |
| GET | `/api/runbooks` | Diagnostic checklist library |
| POST | `/api/search` | TF-IDF retrieval `{query, exclude?}` |
| POST | `/api/anomalies` | Isolation Forest on `{case_id}` or `{content}` |
| POST | `/api/compare` | Compare `{left: caseId, right: caseId}` |
| POST | `/api/workspace/seed` | Idempotently retain six synthetic fixtures |

Updates accept `title`, `status`, `priority`, and `owner`. Status values are
open, investigating, monitoring, resolved and archived. Priorities are critical,
high, medium and low. Owner is a label, not an authenticated identity.

Malformed input returns 400, missing cases 404, stale versions 409, oversized
requests 413, wrong content type 415 and rejected origins/hosts 403. ML endpoints
return an explicit `available: false` when dependencies or sufficient windows
are missing; callers must not treat this as a successful model analysis.

Limits: 4 MiB raw input, 10,000 records per analysis, 500 saved cases, 160-character
titles, 100-character owner labels and 4,000-character notes. Requests themselves
are capped at 8 MiB. List endpoints return metadata, not full record bodies.

Configured accounts require a session for data endpoints (401 if absent).
Role restrictions return 403. Default local mode has no authentication.
Optional bearer ingestion keys grant only POST `/api/ingest`. No tenant isolation
or guaranteed high-load availability is provided. See OPERATIONS.md for the role
matrix, HTTPS reverse-proxy requirements and external-delivery contract.

Live ingestion is bounded to 1,000 records in process memory. The UI displays
the latest 100 and polls every 2.5 seconds. An alert requires at least 10 events
and >=20% ERROR/FATAL in the latest 100 records, independent of timestamps.
It is not a time-window rate, an ML score or a request failure percentage.
Ingestion returns accepted/skipped counts; it never automatically saves a case.

Monitor config fields: `name`, `url`, `interval` (5-3600 seconds),
`expected_status` (100-599), `latency_limit` (1-60000 ms), `failure_threshold`
and `recovery_threshold` (1-20), `cooldown` (0-86400 seconds), `enabled`
and `auto_incident` (booleans, both false by default). Creation requires
`retain: true`. Unknown fields are rejected. Explicit-port HTTP loopback and
explicitly allowlisted public HTTPS port-443 URLs without query/fragment/credentials
are supported. Target URLs cannot be
changed after creation. Setting changes reset consecutive counters.

See MONITORING.md for timing, retention and SDK delivery semantics. Automatic
case creation is independent of the transient ingestion threshold badge.

Analytics includes `activity` (latest 24 observed UTC-hour buckets with events
and errors), `activity_total_hours`, `undated`, and `synthetic_cases` (explicitly
labelled sources). Counts exclude archived cases. Missing hours are not filled;
overlapping imported records may be counted more than once. Activity is saved
log evidence, not a request-rate or production-uptime measurement.
# Live Telemetry API

Version 0.6 adds `/v1/traces` (standard OTLP/HTTP JSON/protobuf), `/api/catalog`,
`/api/telemetry/status`, `/api/telemetry/settings`, `/api/telemetry/evidence`, and
`/api/live-incidents`. See [payloads, authorization and limits](LIVE_TELEMETRY.md).
Durable collection is disabled until an administrator explicitly consents.
# Reliability Operations API

Version 0.7 adds sampled reliability objectives, retained change context, draft incident reviews,
and versioned follow-ups. [Routes, payloads, permissions and measurement semantics](RELIABILITY.md).
No SLO watch automatically sends email or claims a complete production traffic census.
