"""Versioned, environment-scoped service identities and explicit DAG dependencies."""

import re
import uuid
from graphlib import CycleError, TopologicalSorter

from investigator.analysis import redact
from investigator.store import ConflictError


def text(value, limit, required=False):
    if not isinstance(value, str) or len(value.strip()) > limit or any(ord(c) < 32 for c in value) or (required and not value.strip()):
        raise ValueError(f"Expected printable text up to {limit} characters.")
    return redact(value.strip())


def environment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value):
        raise ValueError("Environment must be a lowercase label up to 32 characters.")
    return value


class ServiceCatalog:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS services(id TEXT PRIMARY KEY, name TEXT NOT NULL, environment TEXT NOT NULL,
                    owner TEXT NOT NULL, description TEXT NOT NULL, active INTEGER NOT NULL, version INTEGER NOT NULL,
                    UNIQUE(environment,name));
                CREATE TABLE IF NOT EXISTS dependencies(source TEXT REFERENCES services(id), target TEXT REFERENCES services(id), PRIMARY KEY(source,target));
                CREATE TABLE IF NOT EXISTS monitor_services(monitor_id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id));
            """)

    def list(self, env=None):
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM services ORDER BY environment,name").fetchall()
            edges = list(self.store.db.execute("SELECT * FROM dependencies"))
            bindings = list(self.store.db.execute("SELECT * FROM monitor_services"))
        return [dict(row) | {"active": bool(row["active"]), "dependencies": [e["target"] for e in edges if e["source"] == row["id"]],
                             "monitors": [b["monitor_id"] for b in bindings if b["service_id"] == row["id"]]} for row in rows if env is None or row["environment"] == env]

    def get(self, service_id):
        item = next((s for s in self.list() if s["id"] == service_id), None)
        if item is None:
            raise KeyError("Service not found.")
        return item

    def find(self, name, env):
        return next((s for s in self.list(env) if s["name"] == name and s["active"]), None)

    def create(self, data):
        if not isinstance(data, dict) or set(data) - {"name", "environment", "owner", "description", "dependencies"}:
            raise ValueError("Unknown service fields.")
        name, env = text(data.get("name"), 120, True), environment(data.get("environment", "development"))
        owner, description = text(data.get("owner", ""), 100), text(data.get("description", ""), 600)
        with self.store.transaction():
            if len(self.list()) >= 100:
                raise ValueError("Service limit: 100.")
            if any(s["name"] == name and s["environment"] == env for s in self.list()):
                raise ConflictError("This service identity already exists; edit or reactivate it.")
            service_id = uuid.uuid4().hex[:12]
            self.store.db.execute("INSERT INTO services VALUES(?,?,?,?,?,1,1)", (service_id, name, env, owner, description))
            self._edges(service_id, data.get("dependencies", []))
        return self.get(service_id)

    def update(self, service_id, changes, version):
        if not isinstance(changes, dict) or set(changes) - {"owner", "description", "active", "dependencies"}:
            raise ValueError("Service name/environment are immutable; edit ownership, description, active or dependencies.")
        with self.store.transaction():
            item = self.get(service_id)
            if type(version) is not int or version != item["version"]:
                raise ConflictError("Service changed; refresh before editing.")
            active = changes.get("active", item["active"])
            if type(active) is not bool:
                raise ValueError("Active must be boolean.")
            self.store.db.execute("UPDATE services SET owner=?,description=?,active=?,version=version+1 WHERE id=?",
                                  (text(changes.get("owner", item["owner"]), 100), text(changes.get("description", item["description"]), 600), int(active), service_id))
            if "dependencies" in changes:
                self._edges(service_id, changes["dependencies"])
        return self.get(service_id)

    def _edges(self, service_id, targets):
        if not isinstance(targets, list) or len(targets) > 99 or any(not isinstance(t, str) for t in targets) or len(set(targets)) != len(targets):
            raise ValueError("Dependencies must be distinct service IDs.")
        source = self.get(service_id)
        for target in targets:
            destination = self.get(target)
            if target == service_id or destination["environment"] != source["environment"]:
                raise ValueError("Dependencies must reference another service in the same environment.")
        self.store.db.execute("DELETE FROM dependencies WHERE source=?", (service_id,))
        self.store.db.executemany("INSERT INTO dependencies VALUES(?,?)", [(service_id, target) for target in targets])
        rows = list(self.store.db.execute("SELECT * FROM dependencies"))
        if len(rows) > 300:
            raise ValueError("Dependency limit: 300.")
        graph = {s["id"]: set() for s in self.list()}
        for edge in rows:
            graph[edge["source"]].add(edge["target"])
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError:
            raise ValueError("Dependency cycles are unsupported; no changes were saved.") from None

    def topology(self, env):
        services = self.list(env)
        names = {s["id"]: s["name"] for s in services}
        return list(names.values()), [(s["name"], names[t]) for s in services for t in s["dependencies"] if t in names]

    def bind_monitor(self, monitor_id, service_id):
        self.get(service_id)
        with self.store.transaction():
            self.store.db.execute("INSERT INTO monitor_services VALUES(?,?) ON CONFLICT(monitor_id) DO UPDATE SET service_id=excluded.service_id", (monitor_id, service_id))
        return {"monitor_id": monitor_id, "service_id": service_id}
