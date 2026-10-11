# Active Monitoring (v0.4)

Version 0.4 adds allowlisted public HTTPS checks, optional authenticated roles,
write-only SDK ingestion keys and transactional webhook alerts. See
[operations setup](OPERATIONS.md) for configuration and exact security limits.
The loopback-only runtime description below documents the default local mode;
the operations guide describes its opt-in HTTPS extension.

## Workflow

1. Start the local server and open API monitors.
2. Create a monitor with an explicit-port loopback HTTP health URL.
3. Set interval, expected HTTP status and response-header latency limit.
4. Configure consecutive failure/recovery thresholds and cooldown.
5. Consent to local settings/check retention. Automatic incident retention is
   a separate opt-in checkbox. Scheduled checks are disabled by default.
6. Enable scheduling or use Check now. Paused monitors still allow manual checks.
7. A threshold opens one alert. Repeated failures do not create duplicates.
8. Acknowledge an alert to mark it reviewed, without declaring recovery.
9. Consecutive successful checks recover the alert. Eligible linked cases move
   to monitoring, never automatically to resolved.

For a safe end-to-end demonstration, select Use controlled demo endpoint in
the create dialog. Inject failure changes only that synthetic health endpoint.
Healthy restores it. A monitor needs the configured number of checks to cross
either threshold. These deliberately injected responses are not real outages.

## Semantics

- A failed check has an unexpected status, exceeds the latency limit, or cannot
  establish/read an HTTP response. Latency measures response headers only.
- Sample success percentage uses the last 200 checks, including manual checks.
  It is not time-weighted uptime or an SLO. Missing checks imply unknown state.
- One active alert exists per monitor, including acknowledged alerts. Cooldown
  is measured from the previous alert opening, not from its recovery.
- An automatic case contains the consecutive failure snapshot at alert time.
  Continued checks stay in monitor history/live memory, not appended to the case.
- Recovery adds an operator-review note and moves open/investigating cases to
  monitoring. Manual resolved/archived/monitoring states are not overwritten.
- Monitor settings changes reset consecutive counters and make the next check
  due. The target URL is immutable. Alert identity survives settings changes.
- Settings, counters, check history and alerts persist across restart. Enabled
  monitors resume checks when the server starts. Missed checks are not replayed.
- Limits: 20 monitors, 200 checks per monitor, latest 200 alerts displayed.
  Recovered alerts older than the latest 1,000 alert rows are pruned on recovery;
  linked investigations are not erased. Active alerts are never pruned.

## Security and Runtime

Only `http://127.0.0.1:PORT/path` and localhost equivalents are accepted.
The connection is pinned to 127.0.0.1 without DNS or environment proxies.
No redirects, query strings, credentials, fragments, response-body capture or
custom authentication headers are supported. IPv6, LAN and public targets are
intentionally unavailable in this local single-user release. Do not target
GET endpoints with side effects. This is not a public URL-fetching service.

The single background thread checks due monitors sequentially with a two-second
socket timeout. It is not a distributed scheduler or a hard real-time system;
slow targets can delay other checks. Worker faults are shown on the monitors
view. The process must remain running. Thresholds are rule-based, not ML.

## SDKs

Python 3.11+: `sdk/tracewise.py`. Node.js 18+: `sdk/tracewise.mjs`.
No runtime packages required. Both restrict delivery to an explicit-port
loopback origin and send normalized structured JSON to `/api/ingest`.
Redaction occurs server-side, not before transmission. Do not include secrets.

Python:

```python
import logging
from sdk.tracewise import TracewiseClient, TracewiseHandler

client = TracewiseClient("checkout-api")
logger = logging.getLogger("checkout")
logger.addHandler(TracewiseHandler(client))
client.event("Request completed", status=200, request_id="req-1")
with client.capture(request_id="req-2"):
    perform_checkout()  # Application function; exceptions are logged and rethrown.
```

Node.js:

```javascript
import { TracewiseClient } from "./sdk/tracewise.mjs";
const client = new TracewiseClient("payments-api");
await client.event("Request completed", "INFO", {status: 200});
await client.capture(() => performPayment(), {request_id: "req-2"});
```

Events are capped at 64 KiB. Python emission is synchronous with a one-second
socket timeout. Node emission is asynchronous with a one-second delivery timer.
Failed emission returns false, increments `dropped` and records `last_error`
(Python) or `lastError` (Node). There is no durable retry/spooling queue. Capture
preserves the original application exception. Events enter live memory; they
are not automatically saved as cases. Monitor rules currently apply to HTTP
probe results, not arbitrary SDK event streams.

SDK files are repository modules, not published PyPI/npm packages. Use their
source paths or vendor them into your application while retaining documentation.
