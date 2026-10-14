# Verification

## Multi-Service Observability (v0.5)

195 Python tests and four Node SDK tests pass. New coverage includes real
four-listener trace propagation, parent references, optional span parsing,
nonfinite/bounded duration validation, secret redaction, conservative temporal
correlation, missing/undated evidence, three actual fault mechanisms, worker
consent/config limits/concurrency/cancellation, interrupted restart state,
idempotent correlated case retention, viewer/admin permissions and JSON/Markdown
HTTP exports. A Windows SQLite cleanup failure found during implementation was
fixed by explicitly closing connections before temporary-directory cleanup.

An actual three-seed matrix was launched from the dashboard: seeds 7, 19, 42,
all four scenarios, 12 completed runs, 144 gateway checks and 972 spans. Nine
fault runs produced no missed injected incidents or duplicate detector alerts;
three baseline runs produced no false alerts. This is a small accelerated,
controlled matrix, not production accuracy or load capacity. Measured values
are in reports/mesh-benchmark-2026-10-14.md, not hard-coded into results.

Browser startup initially failed, recovered after one runtime reset. Desktop
experiment controls/progress/results, run-to-trace drill-down, nonblank span
waterfalls/dependency graph, anchor counts, episode disclosure, explicit case
retention consent and successful case creation were verified on isolated port
8792. A database-contention episode linked four request IDs and 19 error-event
references; the resulting saved case retains synthetic provenance. Screenshots
are observability-dependencies.png, observability-experiments.png,
observability-traces.png, observability-mobile.png and observability-traces-mobile.png.
Mobile 390 x 650 views measured equal document/client widths of 375; waterfall
rows were visually inspected without overlap. Viewport overrides were reset.

The preview has a separate SQLite DB and no SMTP configuration. User monitors,
Task API and email settings were not changed. Browser download completion is
not claimed; JSON/Markdown attachment responses are HTTP-tested. Standalone
mesh JSON export is implemented, but its manual Ctrl+C workflow is not separately
browser-tested. Configured topology and heuristic dependency candidates are
not automatic infrastructure discovery, W3C/OpenTelemetry compatibility, live
production alert regrouping or verified root causes. No autonomous repair,
public deployment or production security certification is claimed.

## Independent Task API Integration

Eight additional tests cover task CRUD, actual database-backed health/503 errors,
invalid inputs, Host/Origin/content-type guards, missing routes, body limits,
real HTTP probes, persistent tasks, and actual process stop/restart detection
with monitor recovery. Tests use isolated HTTP servers and temporary databases;
the stop/restart test sends no external email. The development Task API was
started separately on loopback port 8800, its `/health` returned database ready,
and the Task Manager API monitor returned healthy / HTTP 200 after integration.
Existing demo monitors and SMTP settings were not modified. No public hosting
or production reliability is claimed.

On 14 October 2026, the user reported receiving both opened (HTTP 503) and
recovered messages for the correct controlled-demo endpoint. This is user-reported
end-to-end SMTP verification, not independent inbox inspection. An earlier
connection-failure email came from a misconfigured port-8000 monitor and must
not be presented as an injected-demo recovery test.

## SMTP Email Alerts

159 Python tests and four Node SDK tests pass. Sixteen new email tests exercise
environment parsing, disabled/partial configuration, TLS-only ports, mailbox and
event-header injection rejection, secret-safe repr, verified SSL contexts,
STARTTLS-before-authentication, no login on TLS failure, rejected recipients,
recovery subjects, queue retries with secret-safe errors, channel conflicts and
deduplicated monitor-open/recovery email transitions. SMTP clients are mocked;
no real external email or inbox delivery was tested. PowerShell helper parsed
successfully, and the changed JavaScript passed syntax checks.

The first sandbox run blocked existing loopback HTTP and temporary SQLite tests;
the complete suite passed after rerunning with the required local access. A
mailbox parser exception exposed by the new tests was normalized to a safe
validation error. Browser verification of the label change was blocked by the
browser security policy; no visual QA or new screenshot is claimed. SMTP is not
configured in the regular local preview. User app-password setup remains pending.

## Controlled Remote Operations (v0.4)

143 Python tests and four Node SDK tests pass. New coverage exercises optional
account authentication, viewer/operator/admin restrictions, login throttling,
eight-hour expiry, logout, hashed session storage, secure proxy cookies,
cross-origin rejection and write-only ingestion keys, including Python and Node
SDK headers. Public-origin startup without accounts fails closed.

Remote-probe tests use mocked DNS/TLS/HTTP connections: exact-host policy,
private/metadata/multicast/mapped/transition-address rejection, mixed DNS answers,
per-check DNS revalidation, pinned socket addresses, original-host TLS SNI and
non-followed redirects. They do not establish live public endpoint availability.

Delivery tests cover atomic rollback, event-key deduplication, retries/backoff,
terminal failures, manual requeue, queue capacity, restart persistence,
open/recovery transitions and payload minimization. A queue-pruning regression
found during testing was fixed so overflow failures remain visible. Real external
receiver delivery is not configured or claimed. JS syntax checks pass.

Browser QA used disposable in-memory admin/viewer accounts on port 8791, not
user credentials. Login/logout, role-specific disabled monitor controls,
authorized controlled-demo execution, admin operations and account-attributed
mutation records were verified. Mobile 390 x 650 login had equal client/scroll
widths of 390; the viewer dashboard measured 375/375. Desktop 1440 x 650 operations
was visually inspected. No browser console errors observed. Disposable accounts
and QA server were removed by stopping the process; the regular preview remains
on port 8790 with local defaults and no real receiver/remote host configured.

Screenshots: `v4-login-mobile.jpg`, `v4-authenticated-operations.jpg`,
`v4-operations.jpg`. No real root cause, autonomous repair, public deployment,
multi-tenant isolation, load testing or production security certification is
claimed. Earlier sections below are historical verification for prior versions.

## Analytics Redesign

Charcoal navigation, grouped workspace links and a stronger metric hierarchy
replace the all-light presentation. Analytics now visualizes actual saved
hourly evidence, lifecycle counts, service comparisons and recent cases.
All-events/error modes, hourly data disclosure and source labelling were
verified in the browser. Charts were visually nonblank with matching counts.

112 Python tests and three Node SDK tests pass. New cases cover UTC-hour
aggregation, missing-hour abstention, undated exclusion, latest-24 bounds,
archived exclusion and explicit synthetic-source counts. JavaScript syntax
check passed. Desktop 1440 x 650 and mobile 390 x 650 were inspected; mobile
document/client widths both measured 375 pixels. No console errors observed.
Screenshots: `analytics-redesign.jpg`, `analytics-redesign-mobile.jpg`.

## Typography And Workspace Refinement

The interface uses locally available Bahnschrift with Segoe UI/system fallbacks;
no third-party font requests or proprietary font files are bundled. Sidebar,
controls, tables, dialogs and type hierarchy share `web/refinements.css`.
Saved-case priority/error sorting, accurate empty-filter state and reset were
added. Browser checks verified priority ordering (critical case first), unmatched
search and filter reset, plus actual saved/open/high-priority totals.

Desktop 1440 x 650 and mobile 390 x 650 screenshots were successfully inspected
and saved as `refined-desktop.jpg` and `refined-mobile.jpg`. Mobile document and
client widths matched at 375 pixels; incident tables scroll within their own
region. Temporary viewport overrides were reset. JavaScript syntax checks
passed; no browser console errors were observed. No backend behavior changed.

## Light Theme Update

The light neutral/blue/coral palette was visually checked on the monitoring
workspace, configuration dialog and investigation timeline. Canvas colors now
read shared CSS tokens. JavaScript syntax checks passed and no browser console
errors were observed. Screenshot: `theme-light.jpg`. No backend behavior changed.

## Version 0.3

Verified locally on 2026-10-12. 109 Python tests and three Node SDK tests pass.
Coverage includes a real loopback HTTP target, status/latency failure, rejected
remote targets, no redirect following, background scheduling, concurrent checks,
one-alert-per-outage deduplication, acknowledgement, consecutive recovery,
cooldown, persistence, capped check retention and automatic-case rollback.
SDK tests verify delivery, oversized-event rejection, original exception
preservation, logging-format fault isolation and server-side secret redaction.

Browser checks: consented monitor creation, two injected 503 checks opening one
alert and a two-record incident, acknowledgement, evidence navigation, two
healthy checks recovering the alert, linked case monitoring status/recovery
note, Python/Node integration tabs and monitor check-history rendering.
The controlled failure is synthetic, not a real dependency outage.

Scheduled checks were enabled through the UI, produced a fresh 200 observation,
then paused again. Both SDKs delivered labelled synthetic events to the running
preview. Restart preserved settings, checks and alert history. Native desktop
rendering and the nonblank latency canvas were visually inspected; screenshot:
`v3-monitors.jpg`. At the 390-pixel mobile override, document/client widths
matched (375 pixels). The browser screenshot tool failed under viewport
overrides, so v0.3 mobile visual verification remains incomplete. Overrides
were reset. No console errors were observed during these workflows.

The process runs locally with no authentication. Public hosting, external
notifications, durable SDK queues and team workspaces are not implemented.
CI configuration is present; no remote CI run is claimed. Existing download
and cross-browser limitations below still apply.

## Version 0.2

Verified locally on 2026-10-12 using Python with scikit-learn 1.9.1.
82 unit and loopback HTTP integration tests passed. Both `web/app.js` and
`web/platform.js` pass `node --check`. The GitHub Actions workflow is configured
but has not run remotely.

Browser checks covered six persisted synthetic incidents, metadata edits,
notes with activity entries, TF-IDF search, Isolation Forest scoring, exact
signature comparisons, analytics and the five runbooks. A payment timeout
query retrieved the matching fixture with cosine similarity 0.502. The model
flagged eight observed buckets, including all three injected burst buckets;
this is descriptive same-input scoring, not evaluated prediction accuracy.

The controlled API produced 30 live events and 10 injected errors; the
latest-event threshold became active at 33.3%. Pausing and investigating a
snapshot produced the expected 30-record, four-group investigation.

Desktop 1440 x 1000 and mobile 390 x 844 were inspected. Mobile analytics
and saved-incident views had equal document/client widths, with tables confined
to their own scroll region. Screenshots: `v2-analytics.jpg`, `v2-mobile.jpg`.
JSON and Markdown attachment endpoints pass HTTP tests; browser download
completion remains unverified. No cross-browser, load or production-security
certification is implied. All populated preview cases are synthetic.

## Version 0.1 History

Verified locally on 2026-10-12. No production accuracy claims are made.

## Automated

`python -m unittest discover -s tests -q`: **41 tests passed**.

`node --check web/app.js`: passed.

The HTTP integration tests run an isolated loopback server on an ephemeral
port, including the 15-request controlled demo. No internet services are used.

## Browser Checks

- Desktop 1440 x 1000 and mobile 390 x 844 layouts inspected.
- No horizontal page overflow observed on the mobile investigation view.
- Timeline is visibly nonblank with sample records.
- Sample file import produces 14 events, seven errors, and four error groups.
- Controlled demo produces 30 events and 10 injected failures across four groups.
- Evidence links, request-ID correlation, service/level filters and event search
  verified through the visible UI.
- Unsupported demo failure correctly shows Insufficient evidence.
- An imported malformed line and missing timestamp produce a visible warning.
- A controlled credential fixture is redacted. HTML-like log content renders
  as text, not as an executable image element.
- No browser console errors observed during the checked workflows.

## Remaining Limits

- The browser automation download event timed out for the JSON Blob export;
  file delivery was not independently confirmed. Export generation is implemented,
  and the JSON investigation structure is exercised by backend tests. Verify the
  downloaded file in a regular browser before treating export QA as complete.
- No automated cross-browser, screen-reader, or load testing has been performed.
- No real production incident dataset has been evaluated.
- Redaction is best effort, not a privacy or security certification.
- Public deployment, authentication and multi-user storage are not included.

Screenshots show the explicitly labelled synthetic sample investigation.
# Version 0.6 Verification / 2026-10-14

- Full suite: 214 Python tests passed; existing Node SDK suite: 4 tests passed.
- Official OpenTelemetry SDK exporter delivered binary spans to the real HTTP receiver.
- Real loopback HTTP example: one W3C trace, two services, three spans, zero missing parents.
- Tests cover opt-in retention, invalid/gzip-bounded payloads, partial rejection, retries,
  catalog cycle rollback, archive and version checks, environment isolation, authenticated
  role/key scope, restart persistence, incremental late evidence, merge/split stickiness,
  and bound-monitor recovery timeline without automatic resolution.
- Browser verified catalog ownership edit, live owner/status/note update, split/merge and
  retained-evidence waterfall/dependency views. No browser error/warning logs were observed.
- Desktop and 390px mobile screenshots saved under `docs/screenshots/live-*`.
  Mobile document client and scroll widths both measured 375px, with no horizontal page overflow.
- Isolated preview: port 8793, `storage/live-workspace.sqlite3`; controlled example telemetry only.
  The user's email-enabled port-8790 server was not restarted or reconfigured.

These checks establish local behavior, not production scalability, verified causal diagnosis,
real customer outage detection accuracy, or public deployment readiness.
# Version 0.7 Verification / 2026-10-15

- Full suite: 240 Python tests passed. Node SDK: 4 tests passed. New frontend scripts pass syntax checks.
- 22 focused reliability/review tests cover formulas, HTTP classification, client/monitor exclusion,
  latency boundaries, sample minimums, unknown/missing/future evidence, environment isolation,
  before/after comparison abstention, version conflicts, audit history, Markdown escaping,
  multiline-note redaction, merged-source actions and restart persistence.
- New authenticated HTTP coverage verifies admin-only objectives, operator changes/actions,
  read-only viewer access, explicit consent and Markdown/JSON export responses.
- Initial regression run observed intermittent Windows socket resets on rejected POST requests.
  Small rejected bodies now have bounded draining; a 30-request rejection regression passes.
  The subsequent complete 240-test run passed without retrying individual tests.
- Browser verified objective creation, actual error-budget consumption, change marker creation,
  insufficient-evidence comparison, action creation/status update, and incident/review cross-navigation
  with operator notes. Restart preserved the preview objective, change marker and follow-up action.
- Controlled preview used five actual healthy gateway requests and three actual failed requests
  after stopping the temporary tasks listener: 21 retained spans, eight gateway SERVER spans,
  three bad gateway requests. The demo objective minimum of five is not production validation.
- Desktop and 390px mobile screenshots: `docs/screenshots/reliability-*`. Each mobile view measured
  document clientWidth=scrollWidth=375px; internal data tables may scroll horizontally by design.
- A generated controlled-example [Markdown review](reports/reliability-review-2026-10-15.md) is retained.
- Preview: port 8794, `storage/reliability-preview.sqlite3`; no SMTP configuration. The user's
  email-enabled port-8790 server and existing previews were not restarted or reconfigured.

No production SLA, causal deployment effect, complete outage duration, public deployment,
autonomous remediation, or notification-delivery claim follows from these local checks.
# Version 0.8 Verification / 2026-10-15

- Complete regression suite: 258 Python tests passed. New frontend safety module passes Node syntax checks.
- Backup tests cover committed snapshots, manifest/checksum validation, explicit source trust,
  non-overwrite behavior, trigger/schema rejection, malformed archives, legacy base workspaces,
  and restore quarantine without changing the original workspace.
- Limiter tests cover refill, Retry-After, category/client/global scopes, bounded bucket memory,
  idle expiry and concurrent token admission. Authenticated HTTP tests verify backup/safety role
  enforcement, consent, ZIP response, 429 JSON/protobuf responses, forwarded-header isolation,
  and connection-capacity 503 followed by successful recovery.
- End-to-end CLI drill exported the controlled reliability workspace and restored it to the separate
  `storage/safety-preview-v08.sqlite3`. Checks reported quarantined=true, monitors_enabled=false,
  telemetry_enabled=false and pending_delivery_replay=false. No existing database was overwritten.
- Browser verified the admin safety console and explicit-consent backup form; the application reported
  the archive prepared. Browser automation did not return a completed blob-download event, so browser
  file persistence is not independently confirmed. The HTTP ZIP and CLI recovery were verified separately.
- Desktop/mobile safety screenshots are retained; 390px mobile viewport measured clientWidth=scrollWidth=375px.
- Source scan found no configured account email, common GitHub/AWS/private-key signatures or real SMTP
  password assignments in release files. This is a limited pattern scan, not a secret-leak guarantee.
- The user's email-enabled port-8790 server was not restarted or reconfigured. Final safety preview is
  port 8795 with its own restored/quarantined database. Request limits remain single-process controls.

No public hosting, distributed load test, DDoS resilience or production security certification is claimed.
