"""Owner-scoped tasks with optimistic edits, recoverable archive and durable history."""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path

from investigator.store import ConflictError, now


def validate(changes, create=False):
    allowed = {"title", "description", "status", "priority", "due_date", "archived"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise ValueError("Supply valid task fields.")
    result = {"description": "", "status": "todo", "priority": "medium", "due_date": None, "archived": False} | changes if create else dict(changes)
    if create and "title" not in result:
        raise ValueError("Task title is required.")
    for field, limit in (("title", 200), ("description", 4000)):
        if field not in result:
            continue
        value = result[field]
        if not isinstance(value, str) or len(value.strip()) > limit or (field == "title" and not value.strip()) or any(ord(c) < 32 and (field == "title" or c not in "\n\r\t") for c in value):
            raise ValueError(f"Invalid {field}; maximum {limit} characters.")
        result[field] = value.strip()
    if "status" in result and result["status"] not in ("todo", "doing", "done"):
        raise ValueError("Status must be todo, doing or done.")
    if "priority" in result and result["priority"] not in ("low", "medium", "high"):
        raise ValueError("Priority must be low, medium or high.")
    if "archived" in result and type(result["archived"]) is not bool:
        raise ValueError("Archived must be boolean.")
    if "due_date" in result and result["due_date"] is not None:
        value = result["due_date"]
        try:
            if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            raise ValueError("Due date must be YYYY-MM-DD or null.") from None
    return result


class TaskStore:
    def __init__(self, path=":memory:", tracer=None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.lock, self.tracer = threading.RLock(), tracer
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS task_items(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL,
                due_date TEXT, archived INTEGER NOT NULL, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS task_owner ON task_items(owner,archived);
            CREATE TABLE IF NOT EXISTS task_history(id INTEGER PRIMARY KEY, task_id TEXT REFERENCES task_items(id),
                owner TEXT NOT NULL, action TEXT NOT NULL, fields TEXT NOT NULL, timestamp TEXT NOT NULL);
        """)

    @contextmanager
    def operation(self, name):
        if self.tracer:
            from opentelemetry.trace import SpanKind, Status, StatusCode
            context = self.tracer.start_as_current_span("sqlite " + name, kind=SpanKind.CLIENT, record_exception=False, set_status_on_exception=False,
                                                        attributes={"db.system.name": "sqlite", "peer.service": "tasks-database"})
        else:
            context = nullcontext(None)
        with context as span:
            try:
                yield
            except sqlite3.Error:
                if span:
                    span.set_status(Status(StatusCode.ERROR, "Database operation failed"))
                raise

    def _item(self, row):
        if row is None:
            raise KeyError("Task not found.")
        return {key: value for key, value in dict(row).items() if key != "owner"} | {"archived": bool(row["archived"])}

    def list(self, owner, archived=False):
        with self.operation("tasks.list"), self.lock:
            rows = self.db.execute("SELECT * FROM task_items WHERE owner=? AND archived=? ORDER BY updated_at DESC,id LIMIT 1000", (owner, int(archived))).fetchall()
            return [self._item(row) for row in rows]

    def get(self, owner, task_id):
        with self.operation("tasks.get"), self.lock:
            return self._item(self.db.execute("SELECT * FROM task_items WHERE owner=? AND id=?", (owner, task_id)).fetchone())

    def _history(self, owner, task_id, action, fields):
        self.db.execute("INSERT INTO task_history(task_id,owner,action,fields,timestamp) VALUES(?,?,?,?,?)", (task_id, owner, action, json.dumps(sorted(fields)), now()))
        self.db.execute("DELETE FROM task_history WHERE id NOT IN (SELECT id FROM task_history ORDER BY id DESC LIMIT 10000)")

    def create(self, owner, data):
        changes = validate(data, True)
        with self.operation("tasks.create"), self.lock, self.db:
            if self.db.execute("SELECT COUNT(*) FROM task_items WHERE owner=?", (owner,)).fetchone()[0] >= 1000 or self.db.execute("SELECT COUNT(*) FROM task_items").fetchone()[0] >= 5000:
                raise ConflictError("Task storage capacity reached; archived tasks still count.")
            task_id, timestamp = uuid.uuid4().hex, now()
            self.db.execute("INSERT INTO task_items VALUES(?,?,?,?,?,?,?,?,1,?,?)", (task_id, owner, changes["title"], changes["description"], changes["status"], changes["priority"], changes["due_date"], int(changes["archived"]), timestamp, timestamp))
            self._history(owner, task_id, "created", changes)
        return self.get(owner, task_id)

    def update(self, owner, task_id, data, version):
        changes = validate(data)
        with self.operation("tasks.update"), self.lock, self.db:
            row = self.db.execute("SELECT * FROM task_items WHERE owner=? AND id=?", (owner, task_id)).fetchone()
            item = self._item(row)
            if type(version) is not int or version != item["version"]:
                raise ConflictError("Task changed in another session. Refresh before saving.")
            updated = item | changes
            self.db.execute("UPDATE task_items SET title=?,description=?,status=?,priority=?,due_date=?,archived=?,version=version+1,updated_at=? WHERE owner=? AND id=?",
                            (updated["title"], updated["description"], updated["status"], updated["priority"], updated["due_date"], int(updated["archived"]), now(), owner, task_id))
            self._history(owner, task_id, "archived" if changes.get("archived") is True else "restored" if changes.get("archived") is False else "updated", changes)
        return self.get(owner, task_id)

    def history(self, owner, task_id):
        self.get(owner, task_id)
        with self.operation("tasks.history"), self.lock:
            return [dict(row) | {"fields": json.loads(row["fields"])} for row in self.db.execute("SELECT action,fields,timestamp FROM task_history WHERE owner=? AND task_id=? ORDER BY id DESC LIMIT 100", (owner, task_id))]

    def health(self):
        with self.lock:
            self.db.execute("SELECT COUNT(*) FROM task_items").fetchone()

    def close(self):
        with self.lock:
            self.db.close()
