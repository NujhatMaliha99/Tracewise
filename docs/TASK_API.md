# Independent Task Manager API

This is a functioning local application, not the controlled failure endpoint.
It stores tasks in SQLite and exposes real create/read/update/delete operations.
Its health endpoint queries the tasks table and returns 503 if the database
query fails. There is no deliberate failure-injection route in this service.

## Start

Run in a separate terminal from Tracewise so the two processes can stop/restart
independently. Use Python 3.11+; no extra dependencies are required for this API.

```powershell
cd "C:\Users\Hp\Documents\ChatGPT\model 2\api-failure-investigator"
python examples/task_api.py --port 8800
```

The default DB is `storage/task-api.sqlite3`, ignored by Git and separate from
Tracewise case/monitor data. Tasks survive server restart. Override with `--db`
or use `--db :memory:` for disposable data. If a preview is already running on
8800, stop that instance before starting your own terminal-managed instance,
or choose a new port and create a matching monitor. Do not run two instances
against the same database as a production setup.

## Use Tasks

In another PowerShell terminal:

```powershell
$base = 'http://127.0.0.1:8800'
$task = Invoke-RestMethod "$base/tasks" -Method Post -ContentType 'application/json' -Body '{"title":"Finish my API integration"}'
Invoke-RestMethod "$base/tasks"
Invoke-RestMethod "$base/tasks/$($task.id)" -Method Patch -ContentType 'application/json' -Body '{"status":"done"}'
Invoke-RestMethod "$base/health"
```

To permanently delete that test task, explicitly run:

```powershell
Invoke-RestMethod "$base/tasks/$($task.id)" -Method Delete
```

| Method | Route | Result |
| --- | --- | --- |
| GET | `/` | Service identity and route list |
| GET | `/health` | 200 after a successful database query; 503 on DB error |
| GET | `/tasks` | Up to 1,000 stored tasks |
| GET | `/tasks/:id` | Task or 404 |
| POST | `/tasks` | Create `{title,status?}`, HTTP 201 |
| PATCH | `/tasks/:id` | Update `{title?,status?}` |
| DELETE | `/tasks/:id` | Delete one task, or 404 |

Titles contain 1-200 characters. Status is todo, doing or done. Unknown fields,
invalid inputs and empty patches are rejected. Requests are bounded to 4 KiB,
and the database accepts at most 1,000 tasks. SQL values use bound parameters.

## Connect Tracewise

Keep the email-enabled Tracewise process running, then create an API monitor:

- Name: Task Manager API
- URL: `http://127.0.0.1:8800/health`
- Expected status: 200; latency limit: 1,000 ms
- Interval: 10 seconds; failure/recovery thresholds: 2
- Cooldown: 60 seconds; schedule enabled
- Consent to monitor retention; automatic incident snapshots are optional

Do not press Tracewise's demo failure/healthy buttons to test this integration.
Those buttons affect `/monitor-demo`, not the independent Task API.

Stop only the Task API process with Ctrl+C in its own terminal. Leave Tracewise
running. Failed connection checks open an alert and enqueue email after the
configured threshold. Restart the Task API with the same command; successful
checks recover the alert and enqueue recovery mail. Detection can take more
than two intervals because checks are sequential and network operations take
time. Laptop sleep, a stopped Tracewise process or unavailable SMTP can delay
notifications. Observe the delivery queue rather than assuming instant inbox
delivery. Pause obsolete demo/port-8000 monitors to reduce confusing alerts.

The development preview's integration was created through the local API with
automatic incident snapshots off. No SMTP credentials were read or changed.

## Boundaries

This is a real local service integration, not production deployment or evidence
of production reliability. Both services run on one laptop, so Tracewise cannot
observe a laptop-wide outage while it is also down. There is no task user login,
multi-tenant isolation, production server, public exposure, durable distributed
queue, pagination beyond the 1,000-task limit, or tamper-proof audit. It binds
loopback only, checks Host/Origin, and accepts JSON mutations; do not expose it
to the internet. Do not use sensitive production tasks in this example.
