# Real task application

This independent application starts empty. Users create their own tasks; no sample tasks or fault injection routes are installed. SQLite stores task content, per-owner tasks, optimistic edit versions and recoverable archives. A bounded history retains field names, not historical content.

## Local

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-task-app.txt
.venv/Scripts/python.exe -m task_app --port 8801
```

Local mode is loopback-only and has one local owner. Do not use local mode for public hosting.

## Connected local workspace

```powershell
.venv/Scripts/python.exe -m task_app.local_stack --retain-telemetry
```

Open `http://127.0.0.1:8803` for tasks and `http://127.0.0.1:8796/?evidence=live#traces` for Tracewise. This link selects retained live telemetry instead of controlled experiment evidence. Create/edit your own tasks, then refresh the traces view. The official SDK exports real task HTTP requests and actual SQLite operations over loopback OTLP HTTP. There are no pre-filled tasks or generated errors. Healthy requests should not create failure incidents.

The explicit consent flag enables bounded local metadata retention. Separate SQLite files `storage/connected-tasks.sqlite3` and `storage/connected-workspace.sqlite3` preserve local data across restarts. Never start another process against either file while this stack is running. Choose unused ports using `--task-port` and `--receiver-port`; existing email services are not restarted. Both servers stop with Ctrl+C. Background monitors and SMTP are deliberately not started by this launcher.

This is local single-user development, without login, not a publicly exposed service. For public hosting, provision accounts and use the authenticated launchers below.

## Render free web service

Use the Tracewise repository. Build command: `pip install -r requirements-task-app.txt`. Start command: `python -m task_app.render_start`. Health check: `/health`. Select the **Free** instance, not a paid disk or service.

Provision accounts locally using the existing hidden-password command:

```powershell
.venv/Scripts/python.exe -m investigator.access --file storage/task-accounts.json --username YOUR_USERNAME --role operator
```

Upload that JSON as a Render secret file named `task-accounts.json` and set `TASKBOARD_ACCOUNTS_FILE=/etc/secrets/task-accounts.json`. Never commit the file, put passwords in command arguments, or paste them into chat. Task accounts are separate from the Tracewise administrative accounts. Viewers cannot edit; even an administrator only sees their own tasks.

The launcher requires HTTPS origin and accounts. Render supplies `PORT` and `RENDER_EXTERNAL_URL`. No paid resources are provisioned by this document.

**Free-tier limitation:** the filesystem is ephemeral. Redeployment/restart may erase tasks, history and sessions. Idle services sleep and may have cold-start latency. This is a usable portfolio deployment, not reliable persistent production storage. Keep an independent copy of important task content. No background uptime guarantees are provided.

## Actual telemetry

Optional server environment variables:

- `TASKBOARD_OTLP_ENDPOINT=https://YOUR_TRACEWISE_HOST/v1/traces`
- `TRACEWISE_OTLP_KEY`: exporter-only secret matching the Tracewise receiver.
- `TASKBOARD_ENVIRONMENT=portfolio`

In Tracewise, register `tracewise-tasks` and `tasks-database` in the same environment, then enable telemetry retention with an administrator account. Export happens only after user task requests. Health checks, logins and static assets are excluded. Spans contain route templates, methods, status, timing, and SQLite operation names, not titles, descriptions, SQL statements, cookies or passwords. Export configuration does not prove successful delivery: verify received traces in Tracewise.

This app does not automatically create artificial failures. Actual database failures produce HTTP 503; deliberate test failures belong only in isolated tests. Free-tier sleep/data loss is not a simulated application incident.

## Tracewise receiver on Render

`render.yaml` declares two free web services, no disks or paid databases. Both consume the workspace's shared free instance-hour allowance. Verify current allowance in Render before deploying. Do not add an external keep-alive service to defeat idle sleep.

For the receiver, build with `pip install -r requirements.txt` and start with `python scripts/render_tracewise.py`. Provision a separate admin account with `python -m investigator.access --file storage/render-accounts.json --username YOUR_USERNAME --role admin`. Add its JSON as the secret file `tracewise-accounts.json`; set `TRACEWISE_ACCOUNTS_FILE=/etc/secrets/tracewise-accounts.json`. Create a random 32+ character exporter key privately in Render, and put the same key in both services' `TRACEWISE_OTLP_KEY` environment variable. Do not print it or put it in the YAML file.

Secret files must be added in Render; the Blueprint intentionally does not embed their contents. Services fail closed until accounts are configured. Set the task application's OTLP endpoint after the receiver's actual HTTPS hostname is assigned. Tracewise retention and service catalog registration must be enabled after deployment, and again after free-tier storage is lost. No SMTP credentials or local workspace data are copied by this setup. Receiver delivery workers, monitors and traces stop when Render sleeps the service.

The Tracewise launcher retains its existing bounded HTTP server behind Render TLS termination; this is not a production-scale server certification. Use only this authenticated setup for the portfolio deployment, not sensitive customer logs.

## Deployment acceptance checklist

- Confirm the instance plan is Free and the deployed commit is the intended GitHub commit. A reserved URL or dashboard Running label does not prove the application started.
- Verify the build and start logs contain a successful listener, without printing passwords, hashes, exporter keys or environment contents.
- Open the task application's `/health` over HTTPS and confirm `status=ok` and `database=ready`.
- In a fresh browser session, verify task requests require sign-in. Sign in using the separate task account; create/edit/archive/restore a disposable task and verify the optimistic edit conflict behavior.
- In Tracewise, sign in using the admin account, register the two actual services in the `portfolio` environment and explicitly enable metadata retention. Do not use the synthetic workspace seed button to create app evidence.
- Configure the task application's HTTPS OTLP endpoint and matching exporter key privately; restart the task service and make real task requests.
- Refresh Tracewise and verify received `tracewise-tasks` traces and `tasks-database` dependencies. Check that task titles, descriptions and account credentials are absent. Configuration alone does not prove delivery.
- Healthy task traffic must not be presented as a real failure incident. Keep deliberate failure tests local and isolated.
- Do not claim 24/7 monitoring or persistent data on free ephemeral instances. Idle sleep also interrupts background work; lost receiver data requires catalog/retention setup again.
- Add hosted notifications only after separate SMTP/webhook setup and a delivery test. This deployment does not copy local Gmail credentials.

Public deployment, hosted login and hosted email delivery remain unverified until these checks are performed successfully. Local integration results do not establish cloud reliability.
