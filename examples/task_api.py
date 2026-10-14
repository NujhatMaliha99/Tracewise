"""A real local task CRUD service, independently monitored by Tracewise."""

import argparse
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TaskServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, db_path=":memory:"):
        self.lock = threading.RLock()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.db.commit()
        try:
            super().__init__(address, TaskHandler)
        except BaseException:
            self.db.close()
            raise

    def server_close(self):
        super().server_close()
        with self.lock:
            self.db.close()


class TaskHandler(BaseHTTPRequestHandler):
    server_version = "Tracewise-TaskAPI/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def send(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, mutation=False):
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            self.send(403, {"error": "Loopback host required."})
            return False
        if mutation and self.headers.get("Origin") not in (None, "http://" + host):
            self.send(403, {"error": "Same-origin requests required."})
            return False
        return True

    def route(self):
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            return None, None
        parts = parsed.path.strip("/").split("/")
        return parsed.path, parts[1] if len(parts) == 2 and parts[0] == "tasks" else None

    def do_GET(self):
        if not self.allowed():
            return
        path, task_id = self.route()
        try:
            with self.server.lock:
                if path == "/health":
                    self.server.db.execute("SELECT COUNT(*) FROM tasks").fetchone()
                    return self.send(200, {"status": "ok", "service": "task-manager-api", "database": "ready"})
                if path == "/tasks":
                    rows = self.server.db.execute("SELECT * FROM tasks ORDER BY created_at DESC,id LIMIT 1000").fetchall()
                    return self.send(200, {"tasks": [dict(row) for row in rows]})
                if task_id:
                    row = self.server.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    return self.send(200, dict(row)) if row else self.send(404, {"error": "Task not found."})
                if path == "/":
                    return self.send(200, {"service": "Task Manager API", "endpoints": ["GET /health", "GET /tasks", "GET /tasks/:id", "POST /tasks", "PATCH /tasks/:id", "DELETE /tasks/:id"], "mode": "local development; no user authentication"})
                return self.send(404, {"error": "Unknown endpoint."})
        except sqlite3.Error:
            self.send(503, {"error": "Database unavailable."})

    def do_POST(self):
        self.mutate("create")

    def do_PATCH(self):
        self.mutate("update")

    def do_DELETE(self):
        self.mutate("delete")

    def mutate(self, action):
        if not self.allowed(mutation=True):
            return
        path, task_id = self.route()
        if (action == "create" and path != "/tasks") or (action != "create" and not task_id):
            return self.send(404, {"error": "Unknown endpoint."})
        try:
            changes = {}
            if action != "delete":
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.send(415, {"error": "Use application/json."})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self.send(413, {"error": "JSON body must contain 1-4096 bytes."})
                changes = json.loads(self.rfile.read(length))
                if not isinstance(changes, dict) or not changes or set(changes) - {"title", "status"}:
                    raise ValueError("Supply title and/or status only.")
                if action == "create" or "title" in changes:
                    title = changes.get("title")
                    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200 or any(ord(c) < 32 for c in title):
                        raise ValueError("Title must contain 1-200 characters without control characters.")
                    changes["title"] = title.strip()
                if "status" in changes and changes["status"] not in {"todo", "doing", "done"}:
                    raise ValueError("Status must be todo, doing or done.")
            with self.server.lock, self.server.db:
                if action == "create":
                    if self.server.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 1000:
                        return self.send(409, {"error": "Task capacity reached."})
                    task_id, timestamp = uuid.uuid4().hex, now()
                    self.server.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (task_id, changes["title"], changes.get("status", "todo"), timestamp, timestamp))
                else:
                    row = self.server.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if not row:
                        return self.send(404, {"error": "Task not found."})
                    if action == "delete":
                        self.server.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                    else:
                        self.server.db.execute("UPDATE tasks SET title=?,status=?,updated_at=? WHERE id=?", (changes.get("title", row["title"]), changes.get("status", row["status"]), now(), task_id))
                result = {"deleted": task_id} if action == "delete" else dict(self.server.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            self.send(201 if action == "create" else 200, result)
        except (ValueError, TypeError, UnicodeDecodeError):
            self.send(400, {"error": "Invalid task data. Use a 1-200 character title and status todo, doing or done."})
        except sqlite3.Error:
            self.send(503, {"error": "Database unavailable."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the independent local Task Manager API.")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "storage" / "task-api.sqlite3"))
    args = parser.parse_args()
    with TaskServer(("127.0.0.1", args.port), args.db) as server:
        print(f"Task Manager API: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
