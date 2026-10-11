# Tracewise Architecture

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
