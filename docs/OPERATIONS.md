# Controlled Remote Operations (v0.4)

SMTP email is also available as an alternative to webhook delivery. Configure
only one channel; see [email setup](EMAIL.md). Pending messages follow the current
channel, so review existing queues before switching or use an isolated demo DB.

This version can monitor approved public HTTPS endpoints and deliver generic
JSON webhook notifications. It adds shared-workspace login and role enforcement.
It does not turn the Python development server into a managed production service.

## Account Setup

Provision accounts with interactive password prompts. Never put passwords in
command arguments or source control. Passwords are salted scrypt hashes.

```powershell
python -m investigator.access --username nujhat --role admin
python -m investigator.access --username teammate --role operator
python -m investigator.access --username reviewer --role viewer
python server.py --accounts storage/accounts.json --port 8790
```

Passwords require 12-256 characters. Protect the ignored `storage/accounts.json`
with OS permissions. Restart after provisioning, disabling or changing accounts;
restart invalidates all sessions. Remove an account from that file to disable it.
Do not use a real shared password for demonstrations.

| Role | Permissions |
| --- | --- |
| Viewer | Read evidence, export, search, compare and analyze without saving |
| Operator | Viewer plus case edits/notes, ingestion, buffer clearing, demo and alert acknowledgement |
| Admin | Operator plus monitor configuration/checks, failure injection and operations/delivery audit |

All accounts share one workspace. There is no tenant separation, SSO, MFA or
self-service password reset. Owner labels do not grant permissions. Sessions
expire after eight hours, are stored as token hashes in memory, and can be
revoked by logout/restart. Cookies use HttpOnly and SameSite=Strict; Secure is
added when an HTTPS public origin is configured. Local HTTP auth is development
only. Login attempts are globally limited to ten per minute, so one client can
temporarily block other logins. Put per-client throttling at the reverse proxy.

The shield button in the header opens admin-only workspace operations: approved
remote hosts, webhook counts, latest delivery outcomes and attributed successful
mutation records. Failed deliveries can be explicitly requeued there. Records
are bounded to 2,000 mutations; they are not a tamper-proof compliance audit.
Authentication successes/failures are not part of that mutation audit.

## Public HTTPS Monitoring

Set exact hostnames before server startup. Only monitor services you own or have
permission to check; respect their request limits. No wildcards are accepted.

```powershell
$env:TRACEWISE_REMOTE_HOSTS = "api.your-domain.example,status.your-domain.example"
python server.py --accounts storage/accounts.json --port 8790
```

In API monitors, enter `https://api.your-domain.example/health`. The example
domains are placeholders, not working endpoints. HTTPS port 443 only; no URL
credentials, query, fragment, proxies, redirects or custom authorization headers.
Each check resolves DNS again, rejects the entire answer if any address is
non-global/private/loopback/link-local/multicast or an IPv6 transition/mapped
address, and connects directly to the validated address. TLS still verifies the
original hostname and uses it for SNI. Removing a host blocks existing checks;
blocked checks are failed observations, not proof of a remote outage.

Loopback HTTP targets remain available. Runtime socket operations time out after
three seconds for remote probes, but DNS uses the operating-system resolver and
does not have a separate application deadline. The scheduler is sequential and
single-process, not a distributed high-availability scheduler. Add host/network
egress firewall rules as defense in depth. A security review is still required.

## Webhook Alerts

Configure an approved receiver using environment variables, not tracked files:

```powershell
$env:TRACEWISE_WEBHOOK_HOSTS = "hooks.your-domain.example"
$env:TRACEWISE_WEBHOOK_URL = "https://hooks.your-domain.example/your-private-receiver-path"
python server.py --accounts storage/accounts.json --port 8790
```

This is a generic JSON receiver contract, not a built-in Slack/email adapter.
Protect receiver access with a secret HTTPS path; URLs are never returned to the
dashboard or included in stored error details. URL queries and custom headers
are not supported. Use an adapter you control if your provider needs another
payload or authentication mechanism. No external destination is configured by
default and no real notification delivery is claimed until a receiver is tested.

```json
{
  "text": "Tracewise: Checkout / opened / Unexpected HTTP 503",
  "event_id": "alert-id:opened",
  "transition": "opened"
}
```

Only opened/recovered transitions enqueue messages. Payloads contain monitor
names and a bounded reason, not log bodies, target URLs or request credentials;
monitor names can still be sensitive. Enabling the receiver consents to this
external disclosure. Acknowledgement is not recovery and sends no message.

An outbox row commits atomically with the incident transition. The worker tries
one due row per iteration, accepting only HTTP 2xx and never following redirects.
Failures retry after 15, 30, 60 and 120 seconds, then become failed after five
attempts. Retries survive restart; event IDs let the receiver deduplicate.
Delivery is **at least once**, not exactly once: a crash after remote acceptance
and before local commit may send the same event again. Disabled/missing webhook
configuration pauses existing pending rows. Changing the configured receiver
will send pending events to the new receiver; review the backlog first.

At most 1,000 pending and 1,000 terminal delivery records are retained. Overflow
marks oldest pending events failed with QueueCapacity rather than silently
pretending they were sent. Failed rows can be requeued by an admin if capacity
exists. This is not an email/SMS/pager escalation service.

## Authenticated SDK Ingestion

Set a random `TRACEWISE_INGEST_KEY` of at least 32 characters through your secret
manager/environment. It authorizes only POST `/api/ingest`, not reads or case
changes. Keep the SDK endpoint loopback; do not send this key over public HTTP.
Rotation takes effect after server restart. SDK delivery remains non-durable.

```python
import os
from sdk.tracewise import TracewiseClient
client = TracewiseClient("checkout", ingest_key=os.environ["TRACEWISE_INGEST_KEY"])
```

```javascript
import { TracewiseClient } from "./sdk/tracewise.mjs";
const client = new TracewiseClient("checkout", "http://127.0.0.1:8790", process.env.TRACEWISE_INGEST_KEY);
```

## HTTPS Reverse Proxy

The app continues binding 127.0.0.1. Use a TLS reverse proxy on the same host,
set `--public-origin https://tracewise.your-domain.example` and supply `--accounts`.
Startup refuses a public origin without accounts. Preserve the exact public
Host header and request Origin; do not trust arbitrary forwarding headers.
Example Caddy configuration, requiring a real domain/TLS setup:

```caddyfile
tracewise.your-domain.example {
    request_body {
        max_size 8MB
    }
    reverse_proxy 127.0.0.1:8790
}
```

Configure edge connection limits, timeouts, login throttling, TLS, service
supervision, firewall, database backup/restore, OS permissions and retention
policy before external use. This repository has not been deployed or load-tested
as a public service. Netlify static hosting alone cannot run this Python backend.

## Diagnostic and Repair Boundaries

More access and automation do not make a symptom a verified cause. Findings
remain evidence-linked hypotheses and anomaly results remain descriptive. The
system can detect failure, snapshot evidence, notify and observe recovery. It
does not autonomously restart, deploy, roll back, delete data or change APIs.
Final resolution and causal confirmation require an operator and independent
evidence. Unrestricted auto-fix would add operational risk, not verification.

Security references: [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
and [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
