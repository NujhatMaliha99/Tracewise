# Email Alerts

Email is optional and works from a running laptop; a VPS and webhook receiver
are not required. Configure one SMTP sender and one recipient. Opened and
recovered monitor alerts use the existing durable notification queue, cooldown,
deduplication and retry policy. Repeated failing checks do not send repeated mail
for the same active episode. This is not an email escalation/paging service.

## Windows Demo With Personal Gmail

1. Enable Google Account 2-Step Verification and create an app password where
   your account permits it. Do not use your normal Google password. School or
   organization policies can prevent app passwords; use a permitted SMTP provider
   instead. [Google's app-password instructions](https://support.google.com/mail/answer/185833)
2. In the project directory, run the helper below. Enter sender, recipient and
   app password only in its local prompts. The password prompt is hidden.
3. Open the printed local URL. Create a monitor, choose **Use controlled demo
   endpoint**, and use a clearly synthetic name. Set failure/recovery thresholds
   to 2, interval to 5 seconds, consent to retention and enable scheduled checks.
4. Select **Inject failure**. After consecutive failed checks, inspect the alert
   inbox, workspace operations delivery status, and your email inbox/spam folder.
5. Select **Healthy**. Consecutive successful checks enqueue a recovery message.
   Restore healthy state and pause the monitor when finished.

```powershell
pwsh -File scripts/start-email.ps1
```

The helper defaults to `smtp.gmail.com`, implicit TLS on port 465, and local app
port 8790. It prompts interactively, creates no password file, and uses the
Git-ignored `storage/email-demo.sqlite3` instead of an existing webhook backlog.
It does not automatically create monitors, send mail, or expose the app publicly.
After it stops, process environment settings are restored. Secrets are present
in process memory and inherited by the Python child; this is not a secret vault.
Protect access to your laptop. Another local process/user with sufficient access
can potentially read process settings. Local demo mode does not enable login.

Use an independent Python environment with requirements installed, or specify an
existing compatible interpreter through `-Python`. Choose `-Port 8792` if 8790
is occupied. For an authorized provider using STARTTLS:

```powershell
pwsh -File scripts/start-email.ps1 -SmtpHost smtp.your-provider.example -Mode starttls
```

The example hostname is a placeholder. Check your provider's SMTP authentication
policy; this adapter supports username/password SMTP, not OAuth2. Do not disable
account protection to make SMTP work. App-password setup is performed by the user.

## Environment Configuration

For service deployment, provision these through your secret manager/environment:

| Variable | Meaning |
| --- | --- |
| `TRACEWISE_SMTP_HOST` | Administrator-selected SMTP hostname |
| `TRACEWISE_SMTP_USERNAME` | SMTP account username |
| `TRACEWISE_SMTP_PASSWORD` | Provider-issued app password / SMTP credential |
| `TRACEWISE_SMTP_FROM` | One complete sender mailbox |
| `TRACEWISE_SMTP_TO` | One complete recipient mailbox |
| `TRACEWISE_SMTP_MODE` | `ssl` (default) or `starttls` |
| `TRACEWISE_SMTP_PORT` | 465 for ssl; 587 for starttls |

Then start `python server.py` with the usual account/public-origin flags if
needed. These settings are read only at startup, not from dashboard forms.
Plaintext SMTP, header-injected addresses and incomplete settings are rejected.
Verified TLS uses Python's default certificate trust; STARTTLS must succeed
before authentication. SMTP is an administrator-trusted egress destination,
separate from the public-HTTPS monitor hostname policy. No arbitrary SMTP target
or recipient can be supplied through the HTTP API. Socket timeout is five seconds
per operation, not an end-to-end delivery deadline; DNS uses the OS resolver.

SMTP and webhook delivery are mutually exclusive in this version. Startup fails
if both are configured. Existing pending messages follow the currently configured
channel/recipient: review the backlog before switching settings, or use a separate
workspace DB for experiments. No environment or SMTP secrets are stored in SQLite
or returned to the browser. Never commit credentials or paste them into chat.

## Verification And Delivery Meaning

With environment settings already configured, explicitly send one synthetic test:

```powershell
python -m investigator.mail --test
```

This command sends immediately to the configured recipient, outside the retry
queue. Its text explicitly says it is a synthetic test, not a detected outage.
In queued delivery, `sent` / the dashboard's **accepted** count means the SMTP
server accepted the message, not guaranteed inbox delivery. Check spam and your
provider's delivery logs. An eventual bounce is not automatically tracked.
Failures retain only the exception type, never the provider's secret-containing
error text. Failed queue records can be requeued by an admin.

Retrying after ambiguous network errors may produce duplicate mail. Stable
Message-ID values help identify duplicate event messages but do not guarantee
recipient deduplication. Monitor names and summaries are disclosed to the SMTP
provider and recipient; use synthetic names while testing. Raw log bodies and
target URLs are not included. Laptop sleep/shutdown stops monitoring and delivery;
pending queue records resume on restart with the same DB and settings.

Implementation reference: [Python smtplib](https://docs.python.org/3/library/smtplib.html).
Automated tests use mocked TLS/SMTP clients and controlled monitor transitions.
On 14 October 2026, the user reported receiving both opened and recovered emails
after configuring SMTP locally and monitoring the correct controlled-demo URL.
This is user-reported end-to-end verification, not independent inbox inspection;
no credentials or recipient addresses are included in this repository.
