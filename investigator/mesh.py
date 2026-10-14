"""Four actual loopback HTTP services with propagated trace context and bounded spans."""

import http.client
import json
import socket
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from investigator.store import now

SERVICES = ("gateway", "orders", "inventory", "payments")
EDGES = (("gateway", "orders"), ("orders", "inventory"), ("orders", "payments"))


class MeshHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


class ServiceMesh:
    def __init__(self, directory, ports=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "inventory.sqlite3"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS stock(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO stock VALUES('demo-item',100)")
            db.commit()
        self.lock = threading.RLock()
        self.records = deque(maxlen=5000)
        self.delay = 0
        self.database_lock = None
        self.servers, self.threads, self.ports = {}, {}, {}
        try:
            for service in SERVICES:
                self.start_service(service, (ports or {}).get(service, 0))
        except BaseException:
            self.close()
            raise

    def start_service(self, service, port=None):
        if service in self.servers:
            raise ValueError("Service is already running.")
        server = MeshHTTPServer(("127.0.0.1", port if port is not None else self.ports[service]), MeshHandler)
        server.mesh, server.service = self, service
        self.ports[service] = server.server_port
        self.servers[service] = server
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.threads[service] = thread
        thread.start()

    def stop_service(self, service):
        server = self.servers.pop(service, None)
        if server:
            server.shutdown()
            server.server_close()
            self.threads.pop(service).join()

    def emit(self, service, trace_id, span_id, parent, started, start_clock, status, message, kind="server", dependency=""):
        record = {"timestamp": now(), "start_time": started, "duration_ms": round((time.perf_counter() - start_clock) * 1000, 3),
                  "service": service, "request_id": trace_id, "span_id": span_id, "parent_span_id": parent,
                  "span_kind": kind, "dependency": dependency, "status": status,
                  "level": "ERROR" if status >= 500 else "INFO", "message": message, "path": "/work"}
        with self.lock:
            self.records.append(record)

    def call(self, service, dependency, trace_id, parent):
        span_id, started, clock = uuid.uuid4().hex[:16], now(), time.perf_counter()
        connection = http.client.HTTPConnection("127.0.0.1", self.ports[dependency], timeout=.15)
        status, message = 503, "Dependency connection failed"
        try:
            connection.request("GET", "/work", headers={"X-Request-ID": trace_id, "X-Parent-Span": span_id})
            response = connection.getresponse()
            response.read(4096)
            status = response.status
            message = "Dependency completed" if status < 500 else "Dependency returned HTTP " + str(status)
        except (TimeoutError, socket.timeout):
            status, message = 504, "Dependency timed out"
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
            self.emit(service, trace_id, span_id, parent, started, clock, status, message, "client", dependency)
        return status

    def fault(self, scenario):
        if scenario == "payments-timeout":
            self.delay = .23
        elif scenario == "service-down":
            self.stop_service("payments")
        elif scenario == "database-contention":
            self.database_lock = sqlite3.connect(self.database)
            self.database_lock.execute("BEGIN EXCLUSIVE")
        elif scenario != "baseline":
            raise ValueError("Unknown controlled fault.")

    def recover(self):
        self.delay = 0
        if self.database_lock:
            self.database_lock.rollback()
            self.database_lock.close()
            self.database_lock = None
        if "payments" not in self.servers:
            self.start_service("payments")

    def close(self):
        self.delay = 0
        if self.database_lock:
            self.database_lock.rollback()
            self.database_lock.close()
            self.database_lock = None
        for service in list(self.servers):
            self.stop_service(service)


class MeshHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        mesh, service = self.server.mesh, self.server.service
        if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            self.send_error(403)
            return
        if self.path not in {"/work", "/health"}:
            self.send_error(404)
            return
        trace_id = self.headers.get("X-Request-ID", "")
        parent = self.headers.get("X-Parent-Span", "")
        # Only accept bounded hex context, never propagate arbitrary header text.
        if not trace_id or len(trace_id) > 64 or any(c not in "0123456789abcdef" for c in trace_id):
            trace_id = uuid.uuid4().hex
        if len(parent) > 32 or any(c not in "0123456789abcdef" for c in parent):
            parent = ""
        span_id, started, clock = uuid.uuid4().hex[:16], now(), time.perf_counter()
        status, message = 200, "Work completed"
        if service == "inventory":
            try:
                with closing(sqlite3.connect(mesh.database, timeout=.04)) as db:
                    db.execute("SELECT quantity FROM stock WHERE sku='demo-item'").fetchone()
            except sqlite3.Error:
                status, message = 503, "Inventory database query failed or locked"
        elif self.path == "/work":
            if service == "payments":
                time.sleep(mesh.delay)
            else:
                for dependency in ("orders",) if service == "gateway" else ("inventory", "payments"):
                    result = mesh.call(service, dependency, trace_id, span_id)
                    if result >= 500:
                        status, message = result, "Upstream dependency failed"
                        break
        mesh.emit(service, trace_id, span_id, parent, started, clock, status, message)
        payload = json.dumps({"service": service, "status": status, "request_id": trace_id, "span_id": span_id}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # A timed-out caller can disconnect before the slow service finishes.
