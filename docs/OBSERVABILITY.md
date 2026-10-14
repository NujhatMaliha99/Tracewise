# Multi-Service Observability (v0.5)

Three new workspace views add Request traces, Dependency incidents and Fault
experiments. This extension uses real local HTTP services and SQLite faults;
results remain explicitly labelled controlled evidence, not production telemetry.

## Services And Spans

The example topology is gateway -> orders -> inventory/payments. Each service
has its own loopback HTTP listener. Client calls propagate X-Request-ID and
X-Parent-Span; receiving servers create child spans. The collector records start
and finish timestamps, elapsed duration, caller/dependency, status, span ID,
parent ID and error text. There are no external requests or real payments.

Run a standalone healthy mesh in its own terminal:

```powershell
python examples/service_mesh.py
```

The default ports are 8810-8813. GET `http://127.0.0.1:8810/work` traverses the
four services. `/health` checks process readiness; inventory also queries SQLite.
`/work` is a read-only synthetic workload, not a payment/order CRUD API. Use the
separate Task API for real task CRUD. The mesh binds loopback and rejects other
Host headers. Hex trace headers are bounded and sanitized. Up to 5,000 spans
remain in memory. Ctrl+C closes the servers and exports `storage/mesh-spans.json`.
Import that JSON through Request traces. The standalone mesh has no runtime
fault-control HTTP endpoint; faults are only controlled inside isolated trials.

The parser now preserves optional span_id, parent_span_id, dependency,
span_kind, start_time and duration_ms. Legacy logs need none of these fields.
Invalid/nonfinite durations are omitted, and best-effort redaction still runs.
The trace waterfall links each record to its scoped S-number evidence ID and
shows parent IDs in expandable details. Missing parents are counted explicitly.
There is no W3C Trace Context/OpenTelemetry interoperability claim; this version
uses a small explicit custom context protocol. No span timings are fabricated.

## Dependency Correlation

Graph edges are configured for this example, not automatically discovered.
Node counts are span-event counts and edge counts are observed client calls.
Errors are not a live service-health determination: a slow server may return 200
after its caller times out. Missing calls are not successes.

Within each request ID, failing spans are linked exactly. A candidate dependency
anchor is selected from the deepest observed failed client dependencies in the
configured topology (or observed error services if no client evidence exists).
Requests with the same anchor set and errors within 30 seconds of the episode's
first error form one candidate episode. This is a fixed window, not indefinitely
extended by each new error. Different anchors remain separate; undated errors
do not temporal-merge. The first observed failing service is reported separately
from the suspected dependency to avoid conflating collection order with cause.

This heuristic can still merge independent failures at the same dependency and
can be wrong for retries, hidden dependencies, clock skew or incomplete spans.
It produces no root-cause confidence probabilities. Expand an episode to inspect
request/evidence IDs. Saving a correlated case requires retention consent, is
idempotent per experiment/run/episode, and preserves span/request IDs in the
existing investigation store. Cases keep synthetic provenance and use normal
normalization, redaction, notes, lifecycle, retrieval and exports.

Correlation currently operates on imported/experiment span snapshots. It does
not continuously regroup arbitrary production monitor alerts or auto-discover
an organization's service graph. Imported traces are transient unless saved via
the existing log-investigation workflow; experiment artifacts persist on consent.

## Fault Experiments

Use Fault experiments -> Run experiment. Default seeds are 7, 19, 42. Choose
scenarios and explicitly consent to retaining results/spans. Only admins can
start/cancel trials or save correlated experiment cases. Viewers can inspect
results and analyze transient span input. There is one worker per server process;
starting another active job returns 409. Cancellation stops between checks and
cleans up owned services. Restart marks unfinished jobs interrupted rather than
inventing completed results. Up to 50 jobs persist in SQLite.

Each run uses a new temporary inventory database and four ephemeral ports, and
reuses Tracewise's MonitorEngine for actual sequential gateway probes. The
matrix makes 12 manual checks per run, with failure/recovery thresholds 2/2 and
no scheduler. Seeded fault onset is check 3 or 4; recovery is four checks later.
The run then closes/join its service threads and SQLite connections, capturing
late spans before temporary data cleanup. No user monitors, SMTP configuration,
Task API, external endpoints, or production services are modified.

| Scenario | Actual intervention |
| --- | --- |
| baseline | Healthy HTTP dependency calls and inventory queries |
| payments-timeout | Payments handler waits 230ms; caller timeout is 150ms |
| service-down | Stop the payments listener, then restart it on its owned port |
| database-contention | Hold a real SQLite exclusive transaction during inventory reads |

Metrics include elapsed detection/recovery seconds, missed injected episodes,
duplicate detector alerts, baseline false alerts, failed checks and actual
gateway response-header P50/P95. N/A is used when a metric has no applicable
ground truth. P95 is a floor-index empirical quantile. Timing includes HTTP
propagation/OS scheduling; seeds reproduce fault placement and spacing, not
bit-identical latency. The accelerated manual-probe protocol is not a production
interval latency estimate. No production accuracy or stress capacity is claimed.

JSON exports include span evidence, sample observations, configuration and
metrics. Markdown exports summarize measured outcomes and limitations. The
[recorded 12-run report](reports/mesh-benchmark-2026-10-14.md) documents the first
matrix: 144 gateway checks and 972 spans. Its timings are measured, not constants
used by the implementation. Experiments send no real external notifications.

## Deployment And Retention

The normal `server.py` command loads these features after restart. Do not stop
your email-enabled server until ready to re-enter its local SMTP setup. The
development preview uses a separate `storage/observability-preview.sqlite3` on
port 8792 with no SMTP settings; its experiment history is intentionally separate
from the email-demo workspace on port 8790. All storage databases remain ignored
by Git. No new third-party dependency is required. Public production hosting,
distributed trace collection and automatic repair remain outside this version.
