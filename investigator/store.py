"""Local cases retained by explicit save or explicitly enabled monitor automation."""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from investigator.analysis import investigate, redact

STATUSES = {"open", "investigating", "monitoring", "resolved", "archived"}
PRIORITIES = {"critical", "high", "medium", "low"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ConflictError(Exception):
    pass


class CaseStore:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                priority TEXT NOT NULL, owner TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                report TEXT NOT NULL, demo_key TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)

    @contextmanager
    def transaction(self):
        # Automation can compose case, note and alert writes in one outer transaction.
        with self.lock:
            if self.db.in_transaction:
                yield
            else:
                with self.db:
                    yield

    def close(self):
        with self.lock:
            self.db.close()

    def _audit(self, case_id, action, detail):
        self.db.execute("INSERT INTO audit(case_id,action,detail,created_at) VALUES(?,?,?,?)", (case_id, action, json.dumps(redact(detail)), now()))

    def _metadata(self, row):
        result = dict(row)
        report = json.loads(result.pop("report"))
        result["summary"] = report["summary"]
        result["services"] = list(report["services"])
        result["source"] = report.get("source", "Imported logs")
        result["filename"] = report["filename"]
        return result

    def list(self, include_archived=False):
        with self.lock:
            rows = self.db.execute("SELECT * FROM cases " + ("" if include_archived else "WHERE status!='archived' ") + "ORDER BY updated_at DESC").fetchall()
            return [self._metadata(row) for row in rows]

    def get(self, case_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError("Investigation not found.")
            result = self._metadata(row)
            result["report"] = json.loads(row["report"])
            result["notes"] = [dict(r) for r in self.db.execute("SELECT * FROM notes WHERE case_id=? ORDER BY id DESC", (case_id,))]
            result["audit"] = [dict(r) | {"detail": json.loads(r["detail"])} for r in self.db.execute("SELECT * FROM audit WHERE case_id=? ORDER BY id DESC", (case_id,))]
            return result

    def create(self, content, filename, title, priority="medium", source="Imported logs", demo_key=None):
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise ValueError("Invalid priority.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
            raise ValueError("Title must contain 1-160 characters.")
        report = investigate(content, filename)
        report["source"] = str(redact(source))[:200]
        case_id, created = uuid.uuid4().hex[:12], now()
        with self.transaction():
            if demo_key:
                existing = self.db.execute("SELECT id FROM cases WHERE demo_key=?", (demo_key,)).fetchone()
                if existing:
                    return self.get(existing["id"])
            if self.db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] >= 500:
                raise ValueError("Local workspace limit reached: 500 saved investigations.")
            self.db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)", (case_id, redact(title.strip()), "open", priority, "", created, created, 1, json.dumps(report), demo_key))
            self._audit(case_id, "created", {"source": report["source"], "records": report["summary"]["records"]})
        return self.get(case_id)

    def update(self, case_id, changes, version):
        if not isinstance(changes, dict):
            raise ValueError("Changes must be a JSON object.")
        with self.transaction():
            case = self.get(case_id)
            if version != case["version"]:
                raise ConflictError("This investigation changed. Reload before saving.")
            if set(changes) - {"title", "status", "priority", "owner"}:
                raise ValueError("Unsupported incident field.")
            updated = {key: changes.get(key, case[key]) for key in ("title", "status", "priority", "owner")}
            if not isinstance(updated["status"], str) or not isinstance(updated["priority"], str) or updated["status"] not in STATUSES or updated["priority"] not in PRIORITIES:
                raise ValueError("Invalid status or priority.")
            if not isinstance(updated["title"], str) or not 1 <= len(updated["title"].strip()) <= 160:
                raise ValueError("Title must contain 1-160 characters.")
            if not isinstance(updated["owner"], str) or len(updated["owner"]) > 100:
                raise ValueError("Owner must be at most 100 characters.")
            updated = redact(updated)
            self.db.execute("UPDATE cases SET title=?,status=?,priority=?,owner=?,updated_at=?,version=version+1 WHERE id=?", (updated["title"], updated["status"], updated["priority"], updated["owner"], now(), case_id))
            self._audit(case_id, "updated", {key: value for key, value in updated.items() if value != case[key]})
        return self.get(case_id)

    def add_note(self, case_id, body):
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000:
            raise ValueError("Note must contain 1-4,000 characters.")
        with self.transaction():
            self.get(case_id)
            self.db.execute("INSERT INTO notes(case_id,body,created_at) VALUES(?,?,?)", (case_id, redact(body.strip()), now()))
            self.db.execute("UPDATE cases SET updated_at=?,version=version+1 WHERE id=?", (now(), case_id))
            self._audit(case_id, "note_added", {})
        return self.get(case_id)

    def documents(self):
        with self.lock:
            return [self.get(c["id"]) for c in self.list()]

    def analytics(self):
        cases = self.list()
        services, statuses, activity = {}, {}, {}
        undated = 0
        for case in cases:
            statuses[case["status"]] = statuses.get(case["status"], 0) + 1
            report = self.get(case["id"])["report"]
            for record in report["records"]:
                if not record["timestamp"]:
                    undated += 1
                    continue
                hour = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
                bucket = activity.setdefault(hour, {"timestamp": hour, "events": 0, "errors": 0})
                bucket["events"] += 1
                bucket["errors"] += record["level"] in {"ERROR", "FATAL"}
            for service in report["services"]:
                entry = services.setdefault(service, {"service": service, "events": 0, "errors": 0, "investigations": 0})
                entry["investigations"] += 1
                entries = [r for r in report["records"] if r["service"] == service]
                entry["events"] += len(entries)
                entry["errors"] += sum(r["level"] in {"ERROR", "FATAL"} for r in entries)
        return {"cases": len(cases), "status_counts": statuses, "events": sum(c["summary"]["records"] for c in cases), "errors": sum(c["summary"]["errors"] for c in cases), "services": sorted(services.values(), key=lambda s: -s["errors"]), "recent": cases[:8], "activity": [activity[key] for key in sorted(activity)[-24:]], "activity_total_hours": len(activity), "undated": undated, "synthetic_cases": sum("synthetic" in c["source"].lower() for c in cases), "scope": "Saved non-archived investigations; overlapping imports can double-count events."}
