# Reliability Operations / Version 0.7

Three connected work surfaces extend Tracewise: Reliability objectives, Change intelligence,
and Incident reviews. They operate on already-retained evidence, with no new external API,
no autonomous repair, and no extra email notifications. Authentication and existing SMTP remain unchanged.

## Local Preview

```powershell
.\.venv\Scripts\python.exe server.py --port 8794 --db storage/reliability-preview.sqlite3
```

For a controlled demonstration only, explicitly register example services and consent to telemetry:

```powershell
.\.venv\Scripts\python.exe examples/otel_demo.py --endpoint http://127.0.0.1:8794/v1/traces --prepare-workspace --retain --requests 5
.\.venv\Scripts\python.exe examples/otel_demo.py --endpoint http://127.0.0.1:8794/v1/traces --failure --requests 3
```

The first command sends real successful HTTP requests. The second stops the actual temporary tasks
listener before gateway requests. This affects no user service and sends no email. No spans are
invented for the unreachable downstream server. A gateway objective with a demo-only minimum of
five requests illustrates budget consumption; such a tiny sample is not a production validation.

## SLO Semantics

Admin creates an objective for a catalog service, with name, indicator, target percentage (90-99.999),
rolling window (1-720 hours), latency limit (1-60,000 ms), minimum classified requests (1-100,000),
and active state. Editing is version-checked; configuration history preserves the last 2,000 edits
globally (latest 50 shown per objective). Service identity cannot change. Maximum 100 objectives;
pausing keeps the row and does not free capacity. All results are recalculated against the **current**
objective configuration, not a historical series of prior policy decisions.

Only retained `origin=otlp`, `span_kind=server` records for that exact service identity are eligible.
Client spans and health-monitor checks are excluded. Each server span is one observed inbound
request; repeated service visits within one trace can therefore be distinct requests. Duplicate
span IDs were already deduplicated at ingestion. Instrumentation that emits duplicate/nested SERVER
spans can still bias the denominator. Non-HTTP server spans without an error status are unclassified.

- Availability: HTTP 100-499 with no ERROR/FATAL is good; HTTP 5xx or ERROR/FATAL is bad.
- Latency: the request must also have a valid finite nonnegative duration at or below the threshold.
  A known failure is always bad, even with a short or missing duration.
- Unknown success outcomes/durations do not enter the classified denominator. Unknown, missing-time
  or future-dated evidence prevents a `within_target` state once enough classified requests exist.
- Rolling windows use span-end event time, with `(start, end]` boundaries. Undated spans are excluded;
  their count refers to all retained server evidence for the service since no window can be assigned.
- No or too little traffic is `insufficient_data`, never healthy. Paused objectives or archived
  services are `paused`. Collection-disabled state is shown separately: retained historical data can
  still be evaluated, but a green historical sample is not proof of current service health.

For classified requests N, bad requests B, and target fraction T:

```text
observed_good_percent = 100 * (N - B) / N
allowed_bad_requests = N * (1 - T)
budget_consumed_percent = 100 * B / allowed_bad_requests
budget_remaining_percent = max(0, 100 - budget_consumed_percent)
burn_rate = (B / N) / (1 - T)
```

Fractional error allowance is intentional, not rounded into an integer quota. Consumption can
exceed 100%. Zero classified requests gives null percentages, not a fabricated 100% result.
Burn is evaluated independently over 1h and 6h. A dashboard watch requires 2x or more in **both**
windows, each meeting the configured minimum and evidence-quality checks. This simple watch is not
a standard paging policy and sends no notification. The 12-bucket chart shows counts, not time-based uptime.
P95 is nearest-rank over valid observed durations, including failures and unclassified HTTP outcomes.
Minimum sample count is a heuristic, not a confidence interval or statistical guarantee.

Sampling, tail-based export, clock skew, missing instrumentation, duplicate instrumentation,
disabled collection and the existing 10,000-span storage cap can all bias results. No actual SLA,
full-month availability percentage, traffic census, or Prometheus-compatible metric backend is claimed.

## Change Intelligence

Operator/admin can explicitly retain an immutable deployment, configuration, or rollback marker for
a catalog service: title, revision/reference, occurred-at UTC and a bounded best-effort-redacted note.
The clock may be up to five minutes ahead. Maximum 1,000 markers, no automatic eviction. Record a
new explanatory marker for a correction; this is not an integration that deploys or rolls back code.

Compare classified availability server spans in the 30 minutes before and after the marker, using
`(start,end]` windows. An error-rate difference in percentage points is shown only after the full
after window ends, with at least 20 classified requests on each side and no unknown outcomes in
either window. Otherwise it abstains with `insufficient_data`. The record at the exact change time
belongs to the before window. Equal-length windows do not control traffic mix, sampling, concurrent
incidents or other changes; differences are descriptive temporal association, not causal attribution.

## Reviews And Follow-Ups

Every live incident has a read-only assembled draft review with its current evidence summary,
operator notes, nearby service changes (up to 30 minutes around the observed evidence interval),
follow-up actions, and a stable evidence index. JSON and Markdown download endpoints include the
retained incident records; the dashboard renders only the first 200 index entries. Timeline source
limits remain 200 events per incident and 10,000 globally; nearby changes are capped at 50.

Drafts are generated from current state, not signed or immutable postmortems. There is no invented
verified cause, customer impact, fix description or MTTR. Successful checks do not automatically
complete actions or resolve incidents. User review and fact-checking are required before publication.
Markdown escapes user-supplied structural characters; best-effort redaction does not guarantee privacy.

Open incident links back to the matching live incident/environment to edit status, ownership and
operator notes. The live incident's Review button returns to its matching review.

Operator/admin can retain a follow-up with title, owner and optional UTC deadline, then version-check
updates through open, in_progress, done or cancelled. Overdue is calculated, not a notification.
Deadlines may be at most one year ahead. Maximum 2,000 actions. There is no irreversible delete.
Merging incidents keeps source actions and notes discoverable through the target's review; actions
keep their original incident IDs. Splitting requests does not guess which actions to move: they stay
with their source incident. Historical source actions remain editable and changes enter that source's timeline.

## API

All reads require a viewer/operator/admin session when authentication is enabled:

- `GET /api/reliability?environment=development`
- `GET /api/objectives/:id/history`
- `GET /api/changes?environment=development`
- `GET /api/changes/:id` (before/after comparison)
- `GET /api/reviews/:incident_id`, `/:incident_id/report` (Markdown), `/:incident_id/export` (JSON)

Admin writes:

- `POST /api/objectives` with `service_id,name,kind,target,window_hours,latency_ms,min_requests,active`
- `POST /api/objectives/:id` with `config` (all fields except service_id) and `version`

Operator/admin writes:

- `POST /api/changes` with `change:{service_id,kind,title,revision,occurred_at,note}`, `retain:true`
- `POST /api/reviews/:incident_id/actions` with `action:{title,owner,due_at}`, `retain:true`
- `POST /api/followups/:id` with `changes:{title?,owner?,due_at?,status?}`, `version`

Validation errors return 400, unknown items 404, stale versions/name conflicts 409, unauthenticated
reads 401 and unauthorized writes 403. The OTLP exporter key cannot access these APIs. Changes are
audited through the existing bounded workspace audit; SLO edits and follow-ups also have domain history.
Bounded draining of small rejected POST bodies avoids premature socket reset on Windows without
changing access checks; rejected payloads are not parsed or retained.
