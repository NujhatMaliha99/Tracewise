# Tracewise Architecture

Version 0.5 adds an isolated ExperimentManager worker, four actual loopback
HTTP services in mesh.py, and pure trace/correlation transforms in
observability.py. Experiment records persist configuration, actual samples,
spans and measured metrics in the experiments SQLite table. The worker reuses
MonitorEngine without its scheduler or notification channel. It owns only its
temporary listeners/database, closes HTTP threads and SQLite handles, and
supports cooperative cancellation. Startup marks unfinished jobs interrupted.
The browser reads artifacts through authenticated experiment endpoints; case
retention is a separate consented, idempotent write. See OBSERVABILITY.md.

```text
Browser workspace
  |-- import, grouping, evidence, exact request traces
  |-- saved incidents, management, notes, activity trail
  |-- analytics, retrieval, anomaly analysis, comparisons, runbooks
  |-- API monitors, alert inbox, SDK integration examples
  |
HTTP API (same-origin JSON / loopback listener / optional HTTPS reverse proxy)
  |-- analysis.py: parse -> redact -> normalize -> group -> evidence findings
  |-- store.py: SQLite persistence with explicit save and optimistic versions
  |-- intelligence.py: TF-IDF retrieval + Isolation Forest + comparison
  |-- fixtures.py: labelled synthetic multi-service scenarios
  |-- stream.py: locked 1,000-event deque, ingestion and threshold snapshots
  |-- monitoring.py: sequential scheduler -> loopback or approved pinned TLS probe -> transitions
  |-- sdk/: Python/Node telemetry -> local ingestion -> redaction -> live memory
  |-- controlled local HTTP failure endpoint
  |
SQLite workspace (Git-ignored)
  |-- cases: redacted normalized report and lifecycle metadata
  |-- notes: operator notes, best-effort redacted
  |-- audit: local creation/update/note activity
  |-- monitors: validated config, consecutive counters, active-alert identity
  |-- monitor_checks: latest 200 response-header observations per monitor
  |-- monitor_alerts: deduplicated episodes with optional linked case
```

## Data Lifecycle

Import analysis is transient. Save reparses normalized records and reapplies
redaction on the server before persistence. Source positions are therefore
positions in the saved snapshot, not the initial upload. Skipped raw lines are
not retained. Notes and metadata also pass through redaction. Archiving hides
a case by default but does not erase it; a status change can restore it.
Monitor creation requires local retention consent. Its settings/checks persist;
separately opted-in automation creates retained failure snapshots. Probe results
also enter the rolling live buffer. No response bodies are captured.

## Model Lifecycle

Retrieval vectorizers are fitted to the current non-archived saved corpus per
query. The query is transformed separately; no root-cause labels are predicted.
Isolation Forest is fitted per investigation with deterministic settings and
scores the same observed buckets. This is a descriptive tool, not an evaluated
future anomaly detector. Optional dependencies are detected explicitly.

## Consistency

SQLite uses WAL, foreign keys, a shared connection guarded by an RLock, and
transactional case/note/audit updates. The case version increments on edits
and notes. Metadata edits must provide the current version; stale edits fail
with 409. This is a local implementation, not a distributed job system.
Monitor transitions serialize across manual and scheduled checks. Case writes
participate in the outer check/alert transaction; fault injection verifies that
failed automatic creation rolls back checks, counters and partial cases.
One in-process background worker schedules enabled monitors; restart resumes
them without replaying missing observations. Thread faults are exposed to UI.

## Expansion Boundaries

Version 0.4 adds `access.py` (offline-provisioned accounts and hashed in-memory
sessions), `outbound.py` (exact-host HTTPS policy, DNS-address validation and
pinned TLS sockets), and `delivery.py` (transactional notification outbox).
Monitor opening/recovery and outbox insertion share a transaction. The single
worker delivers one due notification per iteration after monitor checks; network
delivery is outside the database lock. A crash after remote acceptance can
redeliver the same event ID. The admin operations view exposes retry outcomes,
configured egress hostnames and successful mutation attribution. See OPERATIONS.md.

Production deployment requires a hardened HTTP backend, authenticated users,
workspace-level authorization, retention/deletion policy, bounded job queues,
request timeouts, persistent-secret handling, TLS, and operational monitoring.
External integrations and real streaming ingestion should use explicit adapters
without bypassing redaction and access boundaries. Local HTTP ingestion exists;
durable production streaming and brokers are not implemented. Generic remote
webhook delivery is implemented, but provider-specific adapters are not.
# Version 0.6 Extension

`catalog.py` stores versioned environment-scoped services, monitor bindings and explicit DAG edges.
`otlp.py` decodes standard OTLP/HTTP using official protobuf schemas; `telemetry.py` stores opt-in
evidence and incrementally assigns complete requests to durable incident candidates. Manual
merge/split assignments are sticky and version-checked. Observer failures remain visible without
interrupting monitor/email operations. `web/telemetry.js` adds catalog, connection and incident views.
The transient SDK buffer remains separate. See [live telemetry boundaries](LIVE_TELEMETRY.md).
# Version 0.7 Extension

`reliability.py` owns versioned SLO configuration, descriptive retained-request statistics, and
immutable service-change markers. `reviews.py` assembles evidence-first reviews and maintains
versioned follow-up actions. Both reuse catalog identities and opt-in telemetry evidence under
the existing SQLite transaction lock; neither adds egress, SMTP behavior, or a background pager.
`web/reliability.js` provides three connected work surfaces with guarded polling and environment-aware
navigation back to live incidents. The data-quality and sampling boundaries are [documented](RELIABILITY.md).
