"""Loopback application with opt-in authenticated operations and controlled egress."""

import argparse
import json
import mimetypes
import os
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from investigator.analysis import MAX_BYTES, investigate, parse_logs
from investigator.experiments import ExperimentManager, markdown_report
from investigator.observability import summarize
from investigator.store import CaseStore, ConflictError
from investigator.intelligence import anomaly_analysis, compare, retrieve, runbooks
from investigator.fixtures import SCENARIOS, fixture
from investigator.stream import LiveBuffer
from investigator.monitoring import MonitorEngine
from investigator.outbound import EgressPolicy
from investigator.access import Access
from investigator.delivery import Delivery
from investigator.mail import SMTPConfig, EmailSender
from investigator.catalog import ServiceCatalog, environment
from investigator.telemetry import TelemetryStore
from investigator import otlp
from investigator.reliability import Reliability
from investigator.reviews import Reviews
from investigator.limits import RateLimits, category
from investigator.backups import snapshot

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"


class InvestigatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path=":memory:", accounts=None, public_origin=None, remote_hosts=(), webhook_url=None, webhook_hosts=(), ingest_key=None, smtp_config=None, telemetry_key=None, rate_limits=None, max_connections=16, start_workers=True):
        if type(max_connections) is not int or not 1 <= max_connections <= 128:
            raise ValueError("Connection limit must be 1-128.")
        self.rate_limits = RateLimits(rate_limits)
        self.connection_limit = max_connections
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        self.connection_denials = 0
        self.connection_lock = threading.Lock()
        if smtp_config and webhook_url:
            raise ValueError("Choose either SMTP email or webhook delivery, not both.")
        self.access = Access(accounts)
        if public_origin:
            parsed = urlparse(public_origin)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("Public origin must be an HTTPS origin without path or credentials.")
            if not self.access.enabled:
                raise ValueError("Accounts are required for a public reverse-proxy origin.")
        if ingest_key is not None and len(ingest_key) < 32:
            raise ValueError("Ingest key must be at least 32 characters.")
        self.public_origin, self.ingest_key = public_origin, ingest_key
        if telemetry_key is not None and len(telemetry_key) < 32:
            raise ValueError("Telemetry key must be at least 32 characters.")
        self.telemetry_key = telemetry_key
        self.egress = EgressPolicy(remote_hosts)
        webhook_policy = EgressPolicy(webhook_hosts)
        if webhook_url:
            webhook_policy.validate(webhook_url)
        super().__init__(address, Handler)
        self.demo_lock = threading.Lock()
        self.backup_lock = threading.Lock()
        self.demo_events = []
        self.demo_token = secrets.token_urlsafe(24)
        self.store = CaseStore(db_path)
        self.stream = LiveBuffer()
        self.monitor_demo_mode = "healthy"
        self.delivery = Delivery(self.store, webhook_policy, webhook_url, email_sender=EmailSender(smtp_config) if smtp_config else None)
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TABLE IF NOT EXISTS access_audit(id INTEGER PRIMARY KEY, timestamp TEXT, username TEXT, role TEXT, path TEXT)")
        self.catalog = ServiceCatalog(self.store)
        self.telemetry = TelemetryStore(self.store, self.catalog)
        self.reliability = Reliability(self.store, self.catalog, self.telemetry)
        self.reviews = Reviews(self.store, self.telemetry, self.reliability)
        self.monitors = MonitorEngine(self.store, self.stream, policy=self.egress, delivery=self.delivery, observer=self.telemetry.on_monitor)
        self.experiments = ExperimentManager(self.store)
        if start_workers:
            self.monitors.start()

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            with self.connection_lock:
                self.connection_denials += 1
            try:
                request.settimeout(.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def server_close(self):
        self.experiments.close()
        self.monitors.stop()
        super().server_close()
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "Investigator/0.8"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self.body_read = False

    def host_allowed(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.public_origin:
            allowed.add(urlparse(self.server.public_origin).netloc)
        return self.headers.get("Host", "") in allowed

    def origin_allowed(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        expected = self.server.public_origin if self.server.public_origin and host == urlparse(self.server.public_origin).netloc else f"http://{host}"
        return origin in (None, expected)

    def authorize(self, path, write=False):
        if path in {"/api/session", "/api/login"} or (not write and (not path.startswith("/api/") or path == "/api/health")):
            return True
        if path == "/api/ingest" and self.server.ingest_key:
            if secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.ingest_key):
                return True
        identity = self.server.access.identity(self.headers.get("Cookie"))
        if identity is None:
            self.send(401, {"error": "Sign in required."})
            return False
        readonly = {"/api/analyze", "/api/search", "/api/anomalies", "/api/compare", "/api/sample", "/api/logout", "/api/traces/analyze"}
        admin = path.startswith(("/api/monitors", "/api/experiments", "/api/catalog", "/api/objectives", "/api/backups")) or path in {"/api/monitor-demo", "/api/operations", "/api/telemetry/settings", "/api/safety"}
        if (admin and identity["role"] != "admin" and (write or path in {"/api/operations", "/api/safety"})) or (write and identity["role"] == "viewer" and path not in readonly):
            self.send(403, {"error": "Your workspace role does not permit this action."})
            return False
        return True

    def permit(self, method, path):
        identity = self.server.access.identity(self.headers.get("Cookie"))
        username = identity["username"] if identity and self.server.access.enabled else "anonymous"
        retry = self.server.rate_limits.check(self.client_address[0] + ":" + username, category(method, path))
        if not retry:
            return True
        headers = {"Retry-After": str(retry)}
        if path == "/v1/traces":
            payload, kind = otlp.response(self.headers.get("Content-Type", "").split(";")[0], error="Request rate exceeded. Retry later.", code=8)
            self.send(429, payload, kind, headers=headers)
        else:
            self.send(429, {"error": "Request rate exceeded. Retry later.", "retry_after": retry}, headers=headers)
        return False

    def audit_write(self):
        identity = self.server.access.identity(self.headers.get("Cookie"))
        with self.server.store.lock, self.server.store.db:
            from investigator.store import now
            self.server.store.db.execute("INSERT INTO access_audit(timestamp,username,role,path) VALUES(?,?,?,?)", (now(), identity["username"] if identity else "SDK ingest key", identity["role"] if identity else "ingest", self.path))
            self.server.store.db.execute("DELETE FROM access_audit WHERE id NOT IN (SELECT id FROM access_audit ORDER BY id DESC LIMIT 2000)")

    def log_message(self, *args):
        pass

    def send(self, code, body, kind="application/json; charset=utf-8", download=False, filename="sample-logs.jsonl", headers=None):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)
        if code >= 400 and self.command == "POST" and not self.body_read and not self.headers.get("Transfer-Encoding"):
            # Unread small request bodies can reset Windows sockets before clients read an error.
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if 0 < size <= 65536:
                    self.wfile.flush()
                    self.connection.settimeout(.1)
                    self.rfile.read(size)
            except (OSError, ValueError):
                pass
            finally:
                self.connection.settimeout(10)

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self.host_allowed():
            return self.send(403, {"error": "Local host required."})
        if not self.permit("GET", parsed.path):
            return
        if not self.authorize(parsed.path):
            return
        if parsed.path == "/api/session":
            identity = self.server.access.identity(self.headers.get("Cookie"))
            return self.send(200, {"enabled": self.server.access.enabled, "identity": {k: identity[k] for k in ("username", "role")} if identity else None})
        if parsed.path == "/api/safety":
            return self.send(200, {"rate_limits": self.server.rate_limits.status(), "connection_limit": self.server.connection_limit,
                                   "connection_denials": self.server.connection_denials, "backup": {"database_limit_mib": 64, "encrypted": False, "restore": "Offline / new database only / quarantined"}})
        if parsed.path in {"/api/reliability", "/api/changes"} or parsed.path.startswith(("/api/objectives/", "/api/changes/", "/api/reviews/")):
            try:
                env = environment(parse_qs(parsed.query).get("environment", ["development"])[0])
                parts = parsed.path.strip("/").split("/")
                if parsed.path == "/api/reliability":
                    result = self.server.reliability.dashboard(env)
                elif parsed.path == "/api/changes":
                    result = {"changes": self.server.reliability.changes(env)}
                elif len(parts) == 4 and parts[1] == "objectives" and parts[3] == "history":
                    result = {"history": self.server.reliability.history(parts[2])}
                elif len(parts) == 3 and parts[1] == "changes":
                    result = self.server.reliability.impact(parts[2])
                elif len(parts) == 3 and parts[1] == "reviews":
                    result = self.server.reviews.report(parts[2])
                elif len(parts) == 4 and parts[1] == "reviews" and parts[3] == "report":
                    return self.send(200, self.server.reviews.markdown(parts[2]).encode(), "text/markdown; charset=utf-8", True, f"incident-review-{parts[2]}.md")
                elif len(parts) == 4 and parts[1] == "reviews" and parts[3] == "export":
                    return self.send(200, self.server.reviews.report(parts[2]), download=True, filename=f"incident-review-{parts[2]}.json")
                else:
                    return self.send(404, {"error": "Unknown reliability endpoint."})
                return self.send(200, result)
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            except KeyError:
                return self.send(404, {"error": "Reliability item not found."})
        if parsed.path in {"/api/catalog", "/api/telemetry/status", "/api/telemetry/evidence", "/api/live-incidents"} or parsed.path.startswith("/api/live-incidents/"):
            try:
                env = environment(parse_qs(parsed.query).get("environment", ["development"])[0])
                if parsed.path == "/api/catalog":
                    result = {"services": self.server.catalog.list()}
                elif parsed.path == "/api/telemetry/status":
                    result = self.server.telemetry.status() | {"key_configured": bool(self.server.telemetry_key), "observer_error": self.server.monitors.observer_error}
                elif parsed.path == "/api/telemetry/evidence":
                    result = self.server.telemetry.evidence(env)
                elif parsed.path == "/api/live-incidents":
                    result = {"incidents": self.server.telemetry.incidents(env)}
                else:
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] != "export"):
                        return self.send(404, {"error": "Unknown endpoint."})
                    result = self.server.telemetry.get(parts[2])
                    return self.send(200, result, download=len(parts) == 4, filename=f"live-incident-{parts[2]}.json")
                return self.send(200, result)
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            except KeyError:
                return self.send(404, {"error": "Live incident not found."})
        if parsed.path == "/api/experiments":
            return self.send(200, {"experiments": self.server.experiments.list()})
        if parsed.path.startswith("/api/experiments/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] not in {"export", "report"}):
                return self.send(404, {"error": "Unknown experiment endpoint."})
            try:
                experiment = self.server.experiments.get(parts[2])
                if len(parts) == 4 and parts[3] == "report":
                    return self.send(200, markdown_report(experiment).encode(), "text/markdown; charset=utf-8", True, f"experiment-{parts[2]}.md")
                return self.send(200, experiment, download=len(parts) == 4, filename=f"experiment-{parts[2]}.json")
            except KeyError:
                return self.send(404, {"error": "Experiment not found."})
        if parsed.path == "/api/operations":
            with self.server.store.lock:
                audit = [dict(r) for r in self.server.store.db.execute("SELECT * FROM access_audit ORDER BY id DESC LIMIT 30")]
            return self.send(200, {"remote_hosts": sorted(self.server.egress.hosts), "delivery": self.server.delivery.status(), "audit": audit, "public_origin": self.server.public_origin})
        if parsed.path in {"/", "/index.html"} and not self.server.access.identity(self.headers.get("Cookie")):
            return self.send(302, b"", headers={"Location": "/login.html"})
        if parsed.path == "/api/cases":
            return self.send(200, {"cases": self.server.store.list(parse_qs(parsed.query).get("archived") == ["1"])})
        if parsed.path == "/api/live":
            return self.send(200, self.server.stream.snapshot())
        if parsed.path == "/api/monitors":
            return self.send(200, {"monitors": self.server.monitors.list(), "worker_error": self.server.monitors.last_error})
        if parsed.path.startswith("/api/monitors/"):
            try:
                return self.send(200, self.server.monitors.get(parsed.path.split("/")[-1]))
            except KeyError:
                return self.send(404, {"error": "Monitor not found."})
        if parsed.path == "/api/alerts":
            return self.send(200, {"alerts": self.server.monitors.alerts()})
        if parsed.path == "/api/monitor-demo":
            return self.send(200, {"mode": self.server.monitor_demo_mode})
        if parsed.path == "/monitor-demo":
            return self.send(200 if self.server.monitor_demo_mode == "healthy" else 503, {"controlled_demo": True, "mode": self.server.monitor_demo_mode})
        if parsed.path == "/api/analytics":
            return self.send(200, self.server.store.analytics())
        if parsed.path == "/api/runbooks":
            return self.send(200, {"runbooks": runbooks()})
        if parsed.path == "/api/capabilities":
            import importlib.util
            return self.send(200, {"ml_available": importlib.util.find_spec("sklearn") is not None, "storage": "SQLite", "auth": "Role-based sessions" if self.server.access.enabled else "Local workspace / authentication disabled", "remote_monitoring": bool(self.server.egress.hosts), "remote_hosts": sorted(self.server.egress.hosts), "version": "0.8.0", "otlp_http_traces": True, "sampled_slos": True, "incident_reviews": True, "workspace_backups": True, "request_limits": True})
        if parsed.path.startswith("/api/cases/"):
            parts = parsed.path.strip("/").split("/")
            try:
                case = self.server.store.get(parts[2])
            except KeyError:
                return self.send(404, {"error": "Investigation not found."})
            if len(parts) == 4 and parts[3] == "export":
                return self.send(200, case, download=True, filename=f"investigation-{case['id']}.json")
            if len(parts) == 4 and parts[3] == "report":
                report = case["report"]
                text = f"# {case['title']}\n\nStatus: {case['status']} | Priority: {case['priority']}\n\n"
                text += f"Source: {case['source']}\n\n{report['summary']['records']} events; {report['summary']['errors']} errors.\n\n"
                for group in report["groups"]:
                    text += f"## {group['service']} / {group['count']} events\n\n"
                    for finding in group["findings"]:
                        text += f"### {finding['title']}\n\n{finding['explanation']}\n\nEvidence: " + ", ".join(finding["evidence_ids"][:50]) + "\n\n"
                text += "## Limitations\n\n" + "\n".join("- " + item for item in report["limitations"])
                return self.send(200, text.encode(), "text/markdown; charset=utf-8", True, f"incident-{case['id']}.md")
            if len(parts) != 3:
                return self.send(404, {"error": "Unknown endpoint."})
            return self.send(200, case)
        if parsed.path == "/api/health":
            return self.send(200, {"status": "ok", "version": "0.8.0", "mode": "authenticated" if self.server.access.enabled else "local"})
        if parsed.path == "/api/sample":
            return self.send(200, (ROOT / "samples" / "checkout.jsonl").read_bytes(), "application/x-ndjson", True)
        if parsed.path.startswith("/demo/"):
            return self.demo_request(parsed)
        if parsed.path.startswith("/api/"):
            return self.send(404, {"error": "Unknown API endpoint."})
        relative = parsed.path.lstrip("/") or "index.html"
        target = (STATIC / relative).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            return self.send(404, {"error": "Not found."})
        kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return self.send(200, target.read_bytes(), kind)

    def do_POST(self):
        if self.path == "/v1/traces":
            return self.receive_otlp()
        host = self.headers.get("Host", "")
        if not self.host_allowed() or not self.origin_allowed():
            return self.send(403, {"error": "Only same-origin local requests are accepted."})
        if not self.permit("POST", self.path):
            return
        if not self.authorize(self.path, write=True):
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "Content-Type must be application/json."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_BYTES * 2:
                return self.send(413, {"error": "Request is empty or too large."})
            raw = self.rfile.read(size)
            self.body_read = True
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            identity = self.server.access.identity(self.headers.get("Cookie"))
            actor = identity["username"] if identity else "exporter"
            if self.path == "/api/backups/export":
                if body != {"include_workspace": True}:
                    raise ValueError("Explicit whole-workspace backup consent is required.")
                if not self.server.backup_lock.acquire(blocking=False):
                    return self.send(409, {"error": "Another workspace backup is in progress."})
                try:
                    archive, manifest = snapshot(self.server.store)
                    self.audit_write()
                    return self.send(200, archive, "application/zip", True, "tracewise-workspace-backup.zip")
                finally:
                    self.server.backup_lock.release()
            elif self.path == "/api/objectives":
                result = self.server.reliability.save(body, actor)
            elif self.path.startswith("/api/objectives/"):
                result = self.server.reliability.save(body.get("config"), actor, self.path.split("/")[-1], body.get("version"))
            elif self.path == "/api/changes":
                result = self.server.reliability.change(body.get("change"), actor, body.get("retain"))
            elif self.path.startswith("/api/reviews/"):
                parts = self.path.strip("/").split("/")
                if len(parts) != 4 or parts[3] != "actions":
                    return self.send(404, {"error": "Unknown review endpoint."})
                result = self.server.reviews.create(parts[2], body.get("action"), actor, body.get("retain"))
            elif self.path.startswith("/api/followups/"):
                result = self.server.reviews.update(self.path.split("/")[-1], body.get("changes"), body.get("version"), actor)
            elif self.path == "/api/catalog":
                result = self.server.catalog.create(body)
            elif self.path == "/api/catalog/bind-monitor":
                self.server.monitors.get(body.get("monitor_id"))
                result = self.server.catalog.bind_monitor(body.get("monitor_id"), body.get("service_id"))
            elif self.path.startswith("/api/catalog/"):
                result = self.server.catalog.update(self.path.split("/")[-1], body.get("changes", {}), body.get("version"))
            elif self.path == "/api/telemetry/settings":
                result = self.server.telemetry.configure(body.get("enabled"), body.get("retain"))
            elif self.path.startswith("/api/live-incidents/"):
                parts = self.path.strip("/").split("/")
                if len(parts) == 3:
                    result = self.server.telemetry.update(parts[2], body.get("changes"), body.get("version"), actor)
                elif len(parts) == 4 and parts[3] == "merge":
                    result = self.server.telemetry.merge(parts[2], body.get("source_id"), body.get("target_version"), body.get("source_version"), actor)
                elif len(parts) == 4 and parts[3] == "split":
                    result = self.server.telemetry.split(parts[2], body.get("trace_ids"), body.get("version"), actor)
                else:
                    return self.send(404, {"error": "Unknown incident endpoint."})
            elif self.path == "/api/login":
                if not self.server.access.enabled:
                    raise ValueError("Authentication is not configured.")
                token = self.server.access.login(body.get("username"), body.get("password"))
                cookie = f"tracewise_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800"
                if self.server.public_origin:
                    cookie += "; Secure"
                return self.send(200, {"signed_in": True}, headers={"Set-Cookie": cookie})
            elif self.path == "/api/logout":
                self.server.access.logout(self.headers.get("Cookie"))
                cookie = "tracewise_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                if self.server.public_origin:
                    cookie += "; Secure"
                return self.send(200, {"signed_out": True}, headers={"Set-Cookie": cookie})
            elif self.path == "/api/traces/analyze":
                records, issues = parse_logs(body.get("content"))
                result = summarize(records) | {"source": "Imported span/log snapshot", "skipped": len(issues)}
            elif self.path == "/api/experiments":
                result = self.server.experiments.start(body.get("config", {}), body.get("retain"))
            elif self.path == "/api/experiments/cancel":
                result = self.server.experiments.cancel()
            elif self.path.startswith("/api/experiments/") and self.path.endswith("/cases"):
                parts = self.path.strip("/").split("/")
                if len(parts) != 4:
                    return self.send(404, {"error": "Unknown experiment endpoint."})
                if body.get("retain") is not True:
                    raise ValueError("Explicit correlated case retention consent is required.")
                result = self.server.experiments.save_episode(parts[2], body.get("run_id"), body.get("episode_id"))
            elif self.path == "/api/operations":
                if body.get("action") != "retry" or not isinstance(body.get("event_key"), str):
                    raise ValueError("Choose a failed delivery to retry.")
                result = self.server.delivery.retry(body["event_key"])
            elif self.path == "/api/monitors":
                result = self.server.monitors.create(body.get("config"), body.get("retain"))
            elif self.path.startswith("/api/monitors/"):
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "check":
                    result = self.server.monitors.check(parts[2])
                elif len(parts) == 3:
                    result = self.server.monitors.update(parts[2], body.get("changes"))
                else:
                    return self.send(404, {"error": "Unknown endpoint."})
            elif self.path.startswith("/api/alerts/") and self.path.endswith("/acknowledge"):
                result = self.server.monitors.acknowledge(self.path.split("/")[3])
            elif self.path == "/api/monitor-demo":
                if body.get("mode") not in ("healthy", "failure"):
                    raise ValueError("Choose healthy or failure.")
                self.server.monitor_demo_mode = body["mode"]
                result = {"mode": self.server.monitor_demo_mode, "controlled_demo": True}
            elif self.path == "/api/analyze":
                result = investigate(body.get("content"), body.get("filename", "uploaded logs"))
            elif self.path == "/api/cases":
                result = self.server.store.create(body.get("content"), body.get("filename", "Normalized snapshot"), body.get("title", ""), body.get("priority", "medium"), body.get("source", "Imported normalized snapshot"))
            elif self.path.startswith("/api/cases/"):
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "notes":
                    result = self.server.store.add_note(parts[2], body.get("body"))
                elif len(parts) == 3:
                    result = self.server.store.update(parts[2], body.get("changes", {}), body.get("version"))
                else:
                    return self.send(404, {"error": "Unknown endpoint."})
            elif self.path == "/api/search":
                result = retrieve(body.get("query"), self.server.store.documents(), body.get("exclude"))
            elif self.path == "/api/anomalies":
                if body.get("case_id"):
                    report = self.server.store.get(body["case_id"])["report"]
                else:
                    report = investigate(body.get("content"), "Current snapshot")
                result = anomaly_analysis(report)
            elif self.path == "/api/compare":
                if body.get("left") == body.get("right"):
                    raise ValueError("Choose two different investigations.")
                result = compare(self.server.store.get(body.get("left")), self.server.store.get(body.get("right")))
            elif self.path == "/api/workspace/seed":
                seeded = []
                for index in range(len(SCENARIOS)):
                    data = fixture(index)
                    seeded.append(self.server.store.create(data["content"], data["filename"], data["title"], data["priority"], "Synthetic multi-service incident fixture", data["key"])["id"])
                result = {"ids": seeded, "count": len(seeded), "source": "Synthetic fixtures, not production incidents"}
            elif self.path == "/api/ingest":
                result = self.server.stream.ingest(body.get("content"))
            elif self.path == "/api/live/clear":
                self.server.stream.clear()
                result = {"cleared": True}
            elif self.path == "/api/sample":
                result = investigate((ROOT / "samples" / "checkout.jsonl").read_text(encoding="utf-8"), "Sample checkout incident")
                result["source"] = "Synthetic fixture, not production evidence"
            elif self.path == "/api/demo":
                if not self.server.demo_lock.acquire(blocking=False):
                    return self.send(409, {"error": "A demo run is already in progress."})
                try:
                    self.server.demo_events = []
                    for scenario in ["healthy"] * 5 + ["timeout"] * 4 + ["connection"] * 3 + ["pool"] * 2 + ["unknown"]:
                        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/demo/checkout?scenario={scenario}", headers={"X-Demo-Token": self.server.demo_token})
                        try:
                            with urllib.request.urlopen(req, timeout=5) as response:
                                response.read()
                        except urllib.error.HTTPError as exc:
                            exc.read()
                    result = investigate("\n".join(json.dumps(e) for e in self.server.demo_events), "Controlled demo API run")
                    result["source"] = "15 local HTTP requests; failures are deliberately injected, not real dependency outages"
                    self.server.stream.ingest("\n".join(json.dumps(e) for e in self.server.demo_events))
                finally:
                    self.server.demo_lock.release()
            else:
                return self.send(404, {"error": "Unknown API endpoint."})
            identity = self.server.access.identity(self.headers.get("Cookie"))
            if self.path not in {"/api/analyze", "/api/search", "/api/anomalies", "/api/compare", "/api/sample"}:
                self.audit_write()
            return self.send(200, result)
        except (ValueError, UnicodeDecodeError) as exc:
            return self.send(400, {"error": str(exc)})
        except KeyError:
            return self.send(404, {"error": "Investigation not found."})
        except ConflictError as exc:
            return self.send(409, {"error": str(exc)})
        except PermissionError as exc:
            return self.send(403, {"error": str(exc)})
        except (urllib.error.URLError, TimeoutError):
            return self.send(503, {"error": "Local demo API could not be reached. Try again."})

    def receive_otlp(self):
        kind = self.headers.get("Content-Type", "").split(";")[0]
        def reply(status, rejected=0, error=None, code=3):
            payload, content_type = otlp.response(kind, rejected, error, code)
            return self.send(status, payload, content_type)
        if not self.host_allowed() or not self.origin_allowed():
            return reply(403, error="Host or origin denied.", code=7)
        if not self.permit("POST", "/v1/traces"):
            return
        identity = self.server.access.identity(self.headers.get("Cookie"))
        key = self.server.telemetry_key
        key_ok = bool(key) and secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key)
        if not key_ok and not identity:
            return reply(401, error="Authentication required.", code=16)
        if not key_ok and identity["role"] not in {"admin", "operator"}:
            return reply(403, error="Exporter or operator permission required.", code=7)
        if kind not in {"application/json", "application/x-protobuf"}:
            return reply(415, error="Use OTLP JSON or protobuf.")
        if not self.server.telemetry.status()["enabled"]:
            return reply(403, error="Durable telemetry retention is disabled.", code=7)
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Use Content-Length, not transfer encoding.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= otlp.MAX_BODY:
                raise OverflowError("Payload exceeds 4 MiB.")
            raw = self.rfile.read(size)
            self.body_read = True
            message = otlp.decode(raw, kind, self.headers.get("Content-Encoding", "identity"))
            records, rejected = otlp.spans(message)
            result = self.server.telemetry.ingest(records, actor=identity["username"] if identity else "OTLP exporter")
            return reply(200, rejected + result["rejected"])
        except OverflowError as exc:
            return reply(413, error=str(exc), code=8)
        except ValueError as exc:
            return reply(400, error=str(exc))
        except PermissionError as exc:
            return reply(403, error=str(exc), code=7)

    def demo_request(self, parsed):
        if self.headers.get("X-Demo-Token") != self.server.demo_token:
            return self.send(403, {"error": "Run the controlled demo from the application."})
        scenario = parse_qs(parsed.query).get("scenario", ["healthy"])[0]
        request_id = "demo-" + secrets.token_hex(6)
        base = {"timestamp": datetime.now(timezone.utc).isoformat(), "service": "checkout-api", "request_id": request_id, "method": "POST", "path": "/checkout"}
        self.server.demo_events.append(base | {"level": "INFO", "message": "Request accepted"})
        failures = {"timeout": (504, "Payment dependency timed out after 3000ms"), "connection": (503, "Inventory dependency ECONNREFUSED at inventory:8080"), "pool": (503, "Database connection pool exhausted; active=20 limit=20"), "unknown": (500, "Unexpected checkout state transition")}
        status, message = failures.get(scenario, (200, "Checkout completed"))
        self.server.demo_events.append(base | {"timestamp": datetime.now(timezone.utc).isoformat(), "level": "ERROR" if status >= 500 else "INFO", "status": status, "message": message})
        return self.send(status, {"request_id": request_id, "status": status, "simulated": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--db", default=str(ROOT / "storage" / "workspace.sqlite3"))
    parser.add_argument("--accounts", help="Offline-provisioned account JSON file")
    parser.add_argument("--public-origin", help="Exact HTTPS reverse-proxy origin; requires accounts")
    args = parser.parse_args()
    accounts = json.loads(Path(args.accounts).read_text()) if args.accounts else None
    with InvestigatorServer(("127.0.0.1", args.port), args.db, accounts=accounts, public_origin=args.public_origin,
                            remote_hosts=os.environ.get("TRACEWISE_REMOTE_HOSTS", "").split(","),
                            webhook_url=os.environ.get("TRACEWISE_WEBHOOK_URL"),
                            webhook_hosts=os.environ.get("TRACEWISE_WEBHOOK_HOSTS", "").split(","),
                            ingest_key=os.environ.get("TRACEWISE_INGEST_KEY"),
                            telemetry_key=os.environ.get("TRACEWISE_OTLP_KEY"),
                            smtp_config=SMTPConfig.from_env(os.environ)) as server:
        print(f"API Failure Investigator: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
