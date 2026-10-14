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

from investigator.analysis import MAX_BYTES, investigate
from investigator.store import CaseStore, ConflictError
from investigator.intelligence import anomaly_analysis, compare, retrieve, runbooks
from investigator.fixtures import SCENARIOS, fixture
from investigator.stream import LiveBuffer
from investigator.monitoring import MonitorEngine
from investigator.outbound import EgressPolicy
from investigator.access import Access
from investigator.delivery import Delivery
from investigator.mail import SMTPConfig, EmailSender

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"


class InvestigatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path=":memory:", accounts=None, public_origin=None, remote_hosts=(), webhook_url=None, webhook_hosts=(), ingest_key=None, smtp_config=None):
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
        self.egress = EgressPolicy(remote_hosts)
        webhook_policy = EgressPolicy(webhook_hosts)
        if webhook_url:
            webhook_policy.validate(webhook_url)
        super().__init__(address, Handler)
        self.demo_lock = threading.Lock()
        self.demo_events = []
        self.demo_token = secrets.token_urlsafe(24)
        self.store = CaseStore(db_path)
        self.stream = LiveBuffer()
        self.monitor_demo_mode = "healthy"
        self.delivery = Delivery(self.store, webhook_policy, webhook_url, email_sender=EmailSender(smtp_config) if smtp_config else None)
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TABLE IF NOT EXISTS access_audit(id INTEGER PRIMARY KEY, timestamp TEXT, username TEXT, role TEXT, path TEXT)")
        self.monitors = MonitorEngine(self.store, self.stream, policy=self.egress, delivery=self.delivery)
        self.monitors.start()

    def server_close(self):
        self.monitors.stop()
        super().server_close()
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "Investigator/0.4"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

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
        readonly = {"/api/analyze", "/api/search", "/api/anomalies", "/api/compare", "/api/sample", "/api/logout"}
        admin = path.startswith("/api/monitors") or path in {"/api/monitor-demo", "/api/operations"}
        if (admin and identity["role"] != "admin" and (write or path == "/api/operations")) or (write and identity["role"] == "viewer" and path not in readonly):
            self.send(403, {"error": "Your workspace role does not permit this action."})
            return False
        return True

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

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self.host_allowed():
            return self.send(403, {"error": "Local host required."})
        if not self.authorize(parsed.path):
            return
        if parsed.path == "/api/session":
            identity = self.server.access.identity(self.headers.get("Cookie"))
            return self.send(200, {"enabled": self.server.access.enabled, "identity": {k: identity[k] for k in ("username", "role")} if identity else None})
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
            return self.send(200, {"ml_available": importlib.util.find_spec("sklearn") is not None, "storage": "SQLite", "auth": "Role-based sessions" if self.server.access.enabled else "Local workspace / authentication disabled", "remote_monitoring": bool(self.server.egress.hosts), "remote_hosts": sorted(self.server.egress.hosts), "version": "0.4.0"})
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
            return self.send(200, {"status": "ok", "version": "0.4.0", "mode": "authenticated" if self.server.access.enabled else "local"})
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
        host = self.headers.get("Host", "")
        if not self.host_allowed() or not self.origin_allowed():
            return self.send(403, {"error": "Only same-origin local requests are accepted."})
        if not self.authorize(self.path, write=True):
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "Content-Type must be application/json."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_BYTES * 2:
                return self.send(413, {"error": "Request is empty or too large."})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            if self.path == "/api/login":
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
                with self.server.store.lock, self.server.store.db:
                    from investigator.store import now
                    self.server.store.db.execute("INSERT INTO access_audit(timestamp,username,role,path) VALUES(?,?,?,?)", (now(), identity["username"] if identity else "SDK ingest key", identity["role"] if identity else "ingest", self.path))
                    self.server.store.db.execute("DELETE FROM access_audit WHERE id NOT IN (SELECT id FROM access_audit ORDER BY id DESC LIMIT 2000)")
            return self.send(200, result)
        except (ValueError, UnicodeDecodeError) as exc:
            return self.send(400, {"error": str(exc)})
        except KeyError:
            return self.send(404, {"error": "Investigation not found."})
        except ConflictError as exc:
            return self.send(409, {"error": str(exc)})
        except (urllib.error.URLError, TimeoutError):
            return self.send(503, {"error": "Local demo API could not be reached. Try again."})

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
                            smtp_config=SMTPConfig.from_env(os.environ)) as server:
        print(f"API Failure Investigator: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
