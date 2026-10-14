# Workspace Safety / v0.8

## Backup

An administrator can use Workspace safety to explicitly download an unencrypted ZIP containing
`workspace.sqlite3` and `manifest.json`. SQLite's backup API takes a consistent committed snapshot,
including WAL-backed data; the live database files are not copied piecemeal. Database limit: 64 MiB.
Only one backup is generated/downloaded at a time. Backup requests also have a 2/minute token bucket.

Manifest includes format, creation time, byte count and SHA-256. The archive contains **all retained
workspace evidence and operational state**, including notes, monitor URLs and notification payloads.
Redaction remains best effort. It excludes external accounts files, sessions, passwords and environment
configuration. Protect the archive with private filesystem permissions and encrypted storage; neither
backup encryption nor cryptographic signing is implemented. SHA-256 detects corruption, not authenticity.

`POST /api/backups/export` requires admin and exactly `{"include_workspace":true}`.
Response is a ZIP attachment. Backup creation enters the existing workspace audit, after snapshot capture.
The audit record does not prove the browser successfully stored the downloaded file.

## Offline Recovery

Never restore an unknown archive. Inspect a trusted backup with:

```powershell
.\.venv\Scripts\python.exe -m investigator.backups storage/my-backup.zip
```

Restore into a **new path**, not the currently running workspace:

```powershell
.\.venv\Scripts\python.exe -m investigator.backups storage/my-backup.zip --output storage/recovered-workspace.sqlite3 --trust-backup
.\.venv\Scripts\python.exe server.py --db storage/recovered-workspace.sqlite3 --port 8796
```

The command checks member names, expansion size, checksum, SQLite header, integrity, foreign keys
and exact current schema definitions. Unexpected tables, views, triggers and indexes are rejected.
Same-version backups are the supported contract; older base-only case workspaces are also accepted.
There is no arbitrary schema migration or in-place restore. A complete validated file is published
with an exclusive hard link; existing databases and sidecars are never intentionally overwritten.
Use a local hard-link-capable filesystem such as NTFS/ext4. POSIX restored file mode is 0600;
on Windows protect the destination directory with appropriate ACLs.

Recovery retains investigation evidence but quarantines operations:

- Every monitor has enabled=false; review target, active alerts and scheduling before reenabling.
- Durable telemetry collection is disabled; deliberate retention consent is required to resume it.
- Pending deliveries become failed with RestoredBackupRequiresReview; no automatic notification replay.
- Queued/running experiments become interrupted, not resumed.
- Accounts, API keys, SMTP configuration and sessions are not recovered from the archive.

Keep the original workspace unchanged while evaluating the recovered copy. Do not run two application
instances against the same database. Recovery is offline: there is no browser file-upload restore route.
No automatic backup schedule, cloud upload, encryption, retention expiry or irreversible purge is added.

## Request Protection

In-memory token buckets apply to GET and POST handling before expensive authorized work. Default
capacities refill continuously over 60 seconds:

| Category | Capacity |
| --- | --- |
| Read (including assets/health) | 240 |
| Write | 120 |
| Analysis/demo/experiment start | 12 |
| Ingestion | 120 |
| Login | 10 |
| Backup | 2 |
| Global (all admitted requests) | 600 |

Categories are fixed, not derived from arbitrary request IDs. Scoped keys use socket peer address
and verified account name; forwarded headers and user-supplied usernames are not trusted. Maximum
4,096 tracked buckets; new scopes are rejected when full instead of evicting active throttles. Idle
buckets expire after at least ten minutes. Access.login's existing global attempt guard still applies.
HTTP 429 includes Retry-After. OTLP throttles preserve application/x-protobuf or application/json
google.rpc.Status with RESOURCE_EXHAUSTED (8), without ingesting rejected spans.

Default active connection cap is 16. Saturation returns HTTP 503 with Retry-After: 1; slot release
occurs in a finally block. A connection can hold a slot until the existing 10-second socket timeout.
The admin safety console exposes aggregate counters, policies and capacity, never client identities.
Constructor-only overrides permit controlled testing; there is no browser setting to disable protections.

These are **single-process prototype safeguards**, not DDoS protection, a distributed quota or a
production web-server substitute. Restarts reset buckets/counters. Proxied unauthenticated clients can
share one peer bucket; configure real per-client throttling at a trusted reverse proxy. TLS, network
firewalling, request-size enforcement and hosting limits remain necessary. In-process login limits
can still be used to temporarily deny login to other users. Load/security certification is not claimed.

Admin `GET /api/safety` supplies the console status. Viewer/operator accounts cannot download backups
or read the safety console; client-side hiding is convenience, not the access boundary.
