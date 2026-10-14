"""Durable live evidence, incremental correlation and operator-controlled merge/split."""

import hashlib
import json
import threading
import uuid
from collections import defaultdict
from graphlib import TopologicalSorter

from investigator.analysis import redact
from investigator.catalog import environment, text
from investigator.observability import is_error, seconds, summarize
from investigator.store import ConflictError, now


class TelemetryStore:
    def __init__(self, store, catalog):
        self.store, self.catalog = store, catalog
        self.lock = threading.RLock()
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS telemetry_settings(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL);
                INSERT OR IGNORE INTO telemetry_settings VALUES(1,0);
                CREATE TABLE IF NOT EXISTS live_incidents(id TEXT PRIMARY KEY, environment TEXT NOT NULL, status TEXT NOT NULL,
                    anchor TEXT NOT NULL, window_start REAL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    owner TEXT NOT NULL, version INTEGER NOT NULL, merged_into TEXT);
                CREATE TABLE IF NOT EXISTS telemetry_spans(id TEXT PRIMARY KEY, environment TEXT NOT NULL, trace_key TEXT NOT NULL,
                    service_id TEXT REFERENCES services(id), span_id TEXT NOT NULL, data TEXT NOT NULL,
                    incident_id TEXT REFERENCES live_incidents(id), received_at TEXT NOT NULL,
                    UNIQUE(environment,trace_key,span_id));
                CREATE INDEX IF NOT EXISTS telemetry_trace ON telemetry_spans(environment,trace_key);
                CREATE INDEX IF NOT EXISTS telemetry_incident ON telemetry_spans(incident_id);
                CREATE TABLE IF NOT EXISTS trace_assignments(environment TEXT NOT NULL, trace_key TEXT NOT NULL,
                    incident_id TEXT NOT NULL REFERENCES live_incidents(id), manual INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(environment,trace_key));
                CREATE TABLE IF NOT EXISTS incident_events(id INTEGER PRIMARY KEY, incident_id TEXT REFERENCES live_incidents(id),
                    kind TEXT NOT NULL, detail TEXT NOT NULL, actor TEXT NOT NULL, timestamp TEXT NOT NULL);
            """)

    def status(self):
        with self.store.lock:
            return {"enabled": bool(self.store.db.execute("SELECT enabled FROM telemetry_settings WHERE id=1").fetchone()[0]),
                    "spans": self.store.db.execute("SELECT COUNT(*) FROM telemetry_spans").fetchone()[0],
                    "incidents": self.store.db.execute("SELECT COUNT(*) FROM live_incidents").fetchone()[0],
                    "span_limit": 10000, "incident_limit": 1000, "window_seconds": 60}

    def configure(self, enabled, retain=False):
        if type(enabled) is not bool or (enabled and retain is not True):
            raise ValueError("Enabling durable telemetry requires explicit retention consent.")
        with self.lock, self.store.transaction():
            self.store.db.execute("UPDATE telemetry_settings SET enabled=? WHERE id=1", (int(enabled),))
        return self.status()

    def _event(self, incident_id, kind, detail, actor="automatic"):
        self.store.db.execute("INSERT INTO incident_events(incident_id,kind,detail,actor,timestamp) VALUES(?,?,?,?,?)",
                              (incident_id, kind, json.dumps(redact(detail)), text(actor, 100), now()))
        self.store.db.execute("DELETE FROM incident_events WHERE id NOT IN (SELECT id FROM incident_events ORDER BY id DESC LIMIT 10000)")

    def _records(self, env, trace_key=None, incident_id=None, limit=10000):
        query, values = "SELECT id,data FROM telemetry_spans WHERE environment=?", [env]
        if trace_key is not None:
            query += " AND trace_key=?"
            values.append(trace_key)
        if incident_id is not None:
            query += " AND incident_id=?"
            values.append(incident_id)
        rows = self.store.db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*values, limit)).fetchall()
        records = [json.loads(row["data"]) | {"evidence_id": row["id"]} for row in reversed(rows)]
        parents = {(r.get("request_id"), r.get("parent_span_id")): r for r in records if r.get("span_kind") == "server" and r.get("parent_span_id")}
        for record in records:
            child = parents.get((record.get("request_id"), record.get("span_id")))
            if child and record.get("span_kind") == "client" and not record.get("dependency"):
                record["dependency"] = child["service"]
        return records

    def _anchor(self, records, env):
        services, edges = self.catalog.topology(env)
        parents = {s: set() for s in services}
        for source, target in edges:
            parents[target].add(source)
        depth = {}
        for service in TopologicalSorter(parents).static_order():
            depth[service] = max((depth[p] + 1 for p in parents[service]), default=0)
        errors = [r for r in records if is_error(r)]
        targets = {r.get("dependency") for r in errors if r.get("span_kind") == "client" and r.get("dependency") in depth}
        candidates = targets or {r["service"] for r in errors if r["service"] in depth}
        deepest = max((depth[s] for s in candidates), default=-1)
        return sorted(s for s in candidates if depth[s] == deepest)

    def ingest(self, records, actor="exporter"):
        if not isinstance(records, list) or len(records) > 2000:
            raise ValueError("Limit: 2,000 evidence records per request.")
        accepted, rejected, duplicate, touched = 0, 0, 0, set()
        with self.lock, self.store.transaction():
            if not self.status()["enabled"]:
                raise PermissionError("Durable telemetry is disabled. Enable retention in workspace settings.")
            # Reserve room for new incident candidates before accepting evidence.
            if self.status()["incidents"] >= 1000:
                return {"accepted": 0, "rejected": len(records), "duplicates": 0}
            for raw in records:
                try:
                    env = environment(raw.get("environment", "development"))
                    service = self.catalog.find(raw["service"], env)
                    if service is None:
                        raise ValueError()
                    trace_key, span_id = raw["request_id"], raw["span_id"]
                    if not isinstance(trace_key, str) or not trace_key or len(trace_key) > 200 or not isinstance(span_id, str) or not span_id or len(span_id) > 150:
                        raise ValueError()
                    exists = self.store.db.execute("SELECT service_id FROM telemetry_spans WHERE environment=? AND trace_key=? AND span_id=?", (env, trace_key, span_id)).fetchone()
                    if exists:
                        if exists["service_id"] != service["id"]:
                            raise ValueError()
                        duplicate += 1
                        continue
                    if self.status()["spans"] >= 10000:
                        raise ValueError()
                    data = redact(raw)
                    self.store.db.execute("INSERT INTO telemetry_spans VALUES(?,?,?,?,?,?,NULL,?)", (uuid.uuid4().hex, env, trace_key, service["id"], span_id, json.dumps(data), now()))
                    accepted += 1
                    touched.add((env, trace_key))
                except (ValueError, KeyError, TypeError):
                    rejected += 1
            def first_observation(key):
                dated = [seconds(r.get("timestamp")) for r in self._records(key[0], trace_key=key[1])]
                return (min((v for v in dated if v is not None), default=float("inf")), key)
            for env, trace_key in sorted(touched, key=first_observation):
                self._correlate(env, trace_key, actor)
        return {"accepted": accepted, "rejected": rejected, "duplicates": duplicate}

    def _correlate(self, env, trace_key, actor):
        records = self._records(env, trace_key=trace_key)
        errors = [r for r in records if is_error(r)]
        assignment = self.store.db.execute("SELECT * FROM trace_assignments WHERE environment=? AND trace_key=?", (env, trace_key)).fetchone()
        if assignment:
            incident_id = assignment["incident_id"]
        elif errors:
            if self.store.db.execute("SELECT COUNT(*) FROM live_incidents").fetchone()[0] >= 1000:
                raise ValueError("Incident capacity reached; evidence transaction rolled back.")
            anchor = json.dumps(self._anchor(records, env))
            dated = [seconds(r.get("timestamp")) for r in errors]
            earliest = min((v for v in dated if v is not None), default=None)
            match = self.store.db.execute("SELECT id FROM live_incidents WHERE environment=? AND anchor=? AND status IN ('open','investigating','monitoring') AND window_start<=? AND window_start>=? ORDER BY rowid DESC LIMIT 1",
                                          (env, anchor, earliest, earliest - 60)).fetchone() if earliest is not None else None
            incident_id = match["id"] if match else uuid.uuid4().hex[:12]
            if match is None:
                timestamp = min((r.get("timestamp") for r in errors if r.get("timestamp")), default=now())
                self.store.db.execute("INSERT INTO live_incidents VALUES(?,?,'open',?,?,?,?,?,1,NULL)", (incident_id, env, anchor, earliest, timestamp, timestamp, ""))
                self._event(incident_id, "opened", {"candidate_dependencies": json.loads(anchor), "trace": trace_key}, actor)
            self.store.db.execute("INSERT INTO trace_assignments VALUES(?,?,?,0)", (env, trace_key, incident_id))
        else:
            return
        cursor = self.store.db.execute("UPDATE telemetry_spans SET incident_id=? WHERE environment=? AND trace_key=? AND incident_id IS NULL", (incident_id, env, trace_key))
        if cursor.rowcount:
            timestamps = [r.get("timestamp") for r in records if r.get("timestamp")]
            if timestamps:
                self.store.db.execute("UPDATE live_incidents SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen,?),version=version+1 WHERE id=?", (min(timestamps), max(timestamps), incident_id))
            else:
                self.store.db.execute("UPDATE live_incidents SET version=version+1 WHERE id=?", (incident_id,))
            self._event(incident_id, "evidence_added", {"records": cursor.rowcount, "trace": trace_key}, actor)
            # Late child spans refine the hypothesis, but never silently merge existing incidents.
            if assignment is None or not assignment["manual"]:
                incident_records = self._records(env, incident_id=incident_id)
                self.store.db.execute("UPDATE live_incidents SET anchor=? WHERE id=?", (json.dumps(self._anchor(incident_records, env)), incident_id))

    def incidents(self, env=None):
        with self.store.lock:
            rows = self.store.db.execute("SELECT i.*,COUNT(s.id) AS evidence_count,COUNT(DISTINCT s.trace_key) AS trace_count FROM live_incidents i LEFT JOIN telemetry_spans s ON i.id=s.incident_id GROUP BY i.id ORDER BY i.last_seen DESC LIMIT 1000").fetchall()
            return [dict(row) | {"anchor": json.loads(row["anchor"])} for row in rows if env is None or row["environment"] == env]

    def get(self, incident_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM live_incidents WHERE id=?", (incident_id,)).fetchone()
            if row is None:
                raise KeyError("Live incident not found.")
            records = self._records(row["environment"], incident_id=incident_id)
            events = [dict(r) | {"detail": json.loads(r["detail"])} for r in self.store.db.execute("SELECT * FROM incident_events WHERE incident_id=? ORDER BY id DESC LIMIT 200", (incident_id,))]
            return dict(row) | {"anchor": json.loads(row["anchor"]), "records": records, "timeline": events,
                                "trace_ids": sorted({r["request_id"] for r in records}), "services": sorted({r["service"] for r in records})}

    def _version(self, incident_id, version):
        item = self.get(incident_id)
        if type(version) is not int or item["version"] != version:
            raise ConflictError("Incident evidence changed; refresh before editing.")
        if item["status"] == "merged":
            raise ValueError("This incident was merged; use its target.")
        return item

    def update(self, incident_id, changes, version, actor):
        if not isinstance(changes, dict) or not changes or set(changes) - {"status", "owner", "note"}:
            raise ValueError("Edit status, owner or note only.")
        with self.lock, self.store.transaction():
            item = self._version(incident_id, version)
            status = changes.get("status", item["status"])
            if not isinstance(status, str) or status not in {"open", "investigating", "monitoring", "resolved"}:
                raise ValueError("Invalid incident status.")
            owner = text(changes.get("owner", item["owner"]), 100)
            note = text(changes.get("note", ""), 2000)
            self.store.db.execute("UPDATE live_incidents SET status=?,owner=?,version=version+1 WHERE id=?", (status, owner, incident_id))
            self._event(incident_id, "operator_update", {"status": status, "owner": owner, "note": note}, actor)
        return self.get(incident_id)

    def merge(self, target_id, source_id, target_version, source_version, actor):
        if target_id == source_id:
            raise ValueError("Choose two distinct incidents.")
        with self.lock, self.store.transaction():
            target, source = self._version(target_id, target_version), self._version(source_id, source_version)
            if target["environment"] != source["environment"]:
                raise ValueError("Cross-environment merging is forbidden.")
            self.store.db.execute("UPDATE telemetry_spans SET incident_id=? WHERE incident_id=?", (target_id, source_id))
            self.store.db.execute("UPDATE trace_assignments SET incident_id=?,manual=1 WHERE incident_id IN (?,?)", (target_id, source_id, target_id))
            self.store.db.execute("UPDATE live_incidents SET status='merged',merged_into=?,version=version+1 WHERE id=?", (target_id, source_id))
            self.store.db.execute("UPDATE live_incidents SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen,?),version=version+1 WHERE id=?", (source["first_seen"], source["last_seen"], target_id))
            self.store.db.execute("UPDATE live_incidents SET anchor=? WHERE id=?", (json.dumps(self._anchor(self._records(target["environment"], incident_id=target_id), target["environment"])), target_id))
            self._event(target_id, "manual_merge", {"source": source_id, "source_trace_ids": source["trace_ids"]}, actor)
            self._event(source_id, "merged_into", {"target": target_id}, actor)
        return self.get(target_id)

    def split(self, incident_id, trace_ids, version, actor):
        if not isinstance(trace_ids, list) or not trace_ids or any(not isinstance(t, str) for t in trace_ids) or len(set(trace_ids)) != len(trace_ids):
            raise ValueError("Choose distinct complete request IDs to split.")
        with self.lock, self.store.transaction():
            item = self._version(incident_id, version)
            if not set(trace_ids) < set(item["trace_ids"]):
                raise ValueError("Split a proper subset of this incident's requests.")
            if self.status()["incidents"] >= 1000:
                raise ValueError("Incident capacity reached.")
            new_id = uuid.uuid4().hex[:12]
            selected = [r for r in item["records"] if r["request_id"] in trace_ids]
            timestamps = [r["timestamp"] for r in selected if r.get("timestamp")]
            first, last = min(timestamps, default=now()), max(timestamps, default=now())
            self.store.db.execute("INSERT INTO live_incidents VALUES(?,?,'open',?,?,?,?,?,1,NULL)", (new_id, item["environment"], json.dumps(self._anchor(selected, item["environment"])), seconds(first), first, last, item["owner"]))
            for trace_id in trace_ids:
                self.store.db.execute("UPDATE telemetry_spans SET incident_id=? WHERE environment=? AND trace_key=?", (new_id, item["environment"], trace_id))
                self.store.db.execute("UPDATE trace_assignments SET incident_id=?,manual=1 WHERE environment=? AND trace_key=?", (new_id, item["environment"], trace_id))
            self.store.db.execute("UPDATE trace_assignments SET manual=1 WHERE incident_id=?", (incident_id,))
            self.store.db.execute("UPDATE live_incidents SET version=version+1 WHERE id=?", (incident_id,))
            self._event(incident_id, "manual_split", {"target": new_id, "trace_ids": trace_ids}, actor)
            self._event(new_id, "split_from", {"source": incident_id, "trace_ids": trace_ids}, actor)
        return self.get(new_id)

    def evidence(self, env):
        with self.store.lock:
            services, edges = self.catalog.topology(env)
            records = self._records(env, limit=1000)
            analysis = summarize(records, services=services, edges=edges)
            return analysis | {"source": "Durable live telemetry / latest 1,000 records", "environment": env, "retention": self.status(), "records": records}

    def on_monitor(self, monitor_id, config, result, alert_id, recovered=False):
        if not alert_id or not self.status()["enabled"]:
            return
        with self.store.lock:
            binding = self.store.db.execute("SELECT service_id FROM monitor_services WHERE monitor_id=?", (monitor_id,)).fetchone()
        if not binding:
            return
        service = self.catalog.get(binding["service_id"])
        trace = "monitor:" + alert_id
        record = {"service": service["name"], "environment": service["environment"], "request_id": trace,
                  "span_id": hashlib.sha256((monitor_id + result["timestamp"]).encode()).hexdigest()[:32],
                  "timestamp": result["timestamp"], "status": result["status"], "duration_ms": result["latency"],
                  "level": "INFO" if result["success"] else "ERROR", "message": "Monitor: " + result["reason"], "origin": "monitor"}
        self.ingest([record], actor="monitor")
        if recovered:
            with self.lock, self.store.transaction():
                assignment = self.store.db.execute("SELECT incident_id FROM trace_assignments WHERE environment=? AND trace_key=?", (service["environment"], trace)).fetchone()
                if assignment:
                    self._event(assignment["incident_id"], "monitor_recovered", {"monitor_id": monitor_id, "alert_id": alert_id}, "monitor")
