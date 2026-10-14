"""Sample-based reliability objectives and change context, never a full traffic census."""

import json
import math
import time
import uuid

from investigator.catalog import text
from investigator.analysis import redact
from investigator.observability import is_error, seconds
from investigator.store import ConflictError, now


def number(value, low, high, name):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number between {low} and {high}.")
    return value


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}.")
    return value


def note_text(value):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise ValueError("Change note must contain at most 1,000 characters without control codes.")
    return redact(value.strip())


def request_sample(records, start, end, kind="availability", latency_ms=1000):
    good, bad, unknown, undated, future = [], [], 0, 0, 0
    latencies = []
    for record in records:
        if record.get("origin") != "otlp" or record.get("span_kind") != "server":
            continue
        timestamp = seconds(record.get("timestamp"))
        if timestamp is None:
            undated += 1
            continue
        if timestamp > end:
            future += 1
            continue
        if timestamp <= start:
            continue
        duration = record.get("duration_ms")
        valid_duration = type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
        if valid_duration:
            latencies.append(duration)
        status = record.get("status")
        failed = is_error(record)
        known_http = type(status) is int and 100 <= status <= 599
        if not failed and (not known_http or (kind == "latency" and not valid_duration)):
            unknown += 1
            continue
        unacceptable = failed or (kind == "latency" and duration > latency_ms)
        (bad if unacceptable else good).append(record.get("evidence_id", ""))
    total = len(good) + len(bad)
    latencies.sort()
    return {"total": total, "good": len(good), "bad": len(bad), "unknown": unknown,
            "undated": undated, "future": future, "good_percent": 100 * len(good) / total if total else None,
            "bad_fraction": len(bad) / total if total else None,
            "p95_ms": latencies[max(0, math.ceil(.95 * len(latencies)) - 1)] if latencies else None,
            "bad_evidence_ids": bad[:100], "bad_evidence_truncated": len(bad) > 100}


class Reliability:
    def __init__(self, store, catalog, telemetry, clock=time.time):
        self.store, self.catalog, self.telemetry, self.clock = store, catalog, telemetry, clock
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS objectives(id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id),
                    name TEXT NOT NULL, kind TEXT NOT NULL, target REAL NOT NULL, window_hours INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL, min_requests INTEGER NOT NULL, active INTEGER NOT NULL,
                    version INTEGER NOT NULL, UNIQUE(service_id,name));
                CREATE TABLE IF NOT EXISTS objective_audit(id INTEGER PRIMARY KEY, objective_id TEXT REFERENCES objectives(id),
                    actor TEXT NOT NULL, timestamp TEXT NOT NULL, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS service_changes(id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id),
                    kind TEXT NOT NULL, title TEXT NOT NULL, revision TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    note TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    def _config(self, data):
        if not isinstance(data, dict) or set(data) != {"name", "kind", "target", "window_hours", "latency_ms", "min_requests", "active"}:
            raise ValueError("Objective requires name, kind, target, window_hours, latency_ms, min_requests and active.")
        result = dict(data)
        result["name"] = text(data["name"], 100, True)
        if data["kind"] not in ("availability", "latency") or type(data["active"]) is not bool:
            raise ValueError("Choose availability or latency, and a boolean active flag.")
        result["target"] = number(data["target"], 90, 99.999, "Target percent")
        result["window_hours"] = integer(data["window_hours"], 1, 720, "Window hours")
        result["latency_ms"] = integer(data["latency_ms"], 1, 60000, "Latency milliseconds")
        result["min_requests"] = integer(data["min_requests"], 1, 100000, "Minimum requests")
        return result

    def _audit(self, item, actor):
        self.store.db.execute("INSERT INTO objective_audit(objective_id,actor,timestamp,config) VALUES(?,?,?,?)",
                              (item["id"], text(actor, 100), now(), json.dumps(item)))
        self.store.db.execute("DELETE FROM objective_audit WHERE id NOT IN (SELECT id FROM objective_audit ORDER BY id DESC LIMIT 2000)")

    def get(self, objective_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM objectives WHERE id=?", (text(objective_id, 12, True),)).fetchone()
            if row is None:
                raise KeyError("Objective not found.")
            return dict(row) | {"active": bool(row["active"])}

    def save(self, data, actor, objective_id=None, version=None):
        if not isinstance(data, dict):
            raise ValueError("Objective must be an object.")
        with self.store.transaction():
            if objective_id:
                prior = self.get(objective_id)
                if type(version) is not int or version != prior["version"]:
                    raise ConflictError("Objective changed; refresh before editing.")
                service_id = prior["service_id"]
                config = self._config(data)
                next_version = prior["version"] + 1
            else:
                if set(data) - {"service_id", "name", "kind", "target", "window_hours", "latency_ms", "min_requests", "active"}:
                    raise ValueError("Unknown objective fields.")
                service_id = text(data.get("service_id"), 12, True)
                self.catalog.get(service_id)
                config = self._config({k: v for k, v in data.items() if k != "service_id"})
                next_version = 1
                if self.store.db.execute("SELECT COUNT(*) FROM objectives").fetchone()[0] >= 100:
                    raise ValueError("Objective limit: 100. Archive or edit existing objectives.")
            duplicate = self.store.db.execute("SELECT id FROM objectives WHERE service_id=? AND name=?", (service_id, config["name"])).fetchone()
            if duplicate and duplicate["id"] != objective_id:
                raise ConflictError("This objective name already exists for the service.")
            objective_id = objective_id or uuid.uuid4().hex[:12]
            item = config | {"id": objective_id, "service_id": service_id, "version": next_version}
            self.store.db.execute("INSERT INTO objectives(id,service_id,name,kind,target,window_hours,latency_ms,min_requests,active,version) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,target=excluded.target,window_hours=excluded.window_hours,latency_ms=excluded.latency_ms,min_requests=excluded.min_requests,active=excluded.active,version=excluded.version",
                                  tuple(item[k] for k in ("id", "service_id", "name", "kind", "target", "window_hours", "latency_ms", "min_requests", "active", "version")))
            self._audit(item, actor)
        return self.get(objective_id)

    def records(self, service_id):
        rows = self.store.db.execute("SELECT id,data FROM telemetry_spans WHERE service_id=? ORDER BY rowid", (service_id,)).fetchall()
        return [json.loads(row["data"]) | {"evidence_id": row["id"]} for row in rows]

    def evaluate(self, item, records=None, at=None):
        at = self.clock() if at is None else at
        service = self.catalog.get(item["service_id"])
        records = self.records(item["service_id"]) if records is None else records
        sample = request_sample(records, at - item["window_hours"] * 3600, at, item["kind"], item["latency_ms"])
        allowance = sample["total"] * (1 - item["target"] / 100)
        consumed = sample["bad"] / allowance * 100 if allowance else None
        incomplete = any(sample[key] for key in ("unknown", "undated", "future"))
        state = "insufficient_data" if sample["total"] < item["min_requests"] else "incomplete_data" if incomplete else "breached" if sample["good_percent"] < item["target"] else "within_target"
        if not item["active"] or not service["active"]:
            state = "paused"
        burn = {}
        for hours in (1, 6):
            window = request_sample(records, at - hours * 3600, at, item["kind"], item["latency_ms"])
            window["rate"] = window["bad_fraction"] / (1 - item["target"] / 100) if window["total"] >= item["min_requests"] and not any(window[k] for k in ("unknown", "undated", "future")) else None
            burn[str(hours) + "h"] = window
        # A watch signal is a sampled dashboard state, not an external paging policy.
        watch = all(burn[key]["rate"] is not None and burn[key]["rate"] >= 2 for key in ("1h", "6h"))
        buckets = []
        width = item["window_hours"] * 3600 / 12
        for index in range(12):
            end = at - width * (11 - index)
            bucket = request_sample(records, end - width, end, item["kind"], item["latency_ms"])
            buckets.append({"end_unix": end, "total": bucket["total"], "bad": bucket["bad"], "good_percent": bucket["good_percent"]})
        return item | {"service": service["name"], "environment": service["environment"], "owner": service["owner"],
                       "state": state, "sample": sample, "allowed_bad_requests": allowance,
                       "budget_consumed_percent": consumed, "budget_remaining_percent": max(0, 100 - consumed) if consumed is not None else None,
                       "burn": burn, "burn_watch": watch and state != "paused", "buckets": buckets}

    def dashboard(self, env):
        with self.store.lock:
            at = self.clock()
            services = self.catalog.list(env)
            rows = list(self.store.db.execute("SELECT id FROM objectives ORDER BY name,id"))
            items = [self.get(r["id"]) for r in rows]
            cache = {s["id"]: self.records(s["id"]) for s in services}
            return {"objectives": [self.evaluate(i, cache[i["service_id"]], at) for i in items if i["service_id"] in cache],
                    "evaluated_at": at, "retention_enabled": self.telemetry.status()["enabled"],
                    "basis": "Retained OTLP server spans, not all production traffic. Clients and monitor checks excluded. Sampling and retention limits can bias results."}

    def history(self, objective_id):
        self.get(objective_id)
        with self.store.lock:
            return [dict(r) | {"config": json.loads(r["config"])} for r in self.store.db.execute("SELECT * FROM objective_audit WHERE objective_id=? ORDER BY id DESC LIMIT 50", (objective_id,))]

    def change(self, data, actor, retain=False):
        if retain is not True or not isinstance(data, dict) or set(data) != {"service_id", "kind", "title", "revision", "occurred_at", "note"}:
            raise ValueError("Explicit retention and complete change fields are required.")
        service_id = text(data["service_id"], 12, True)
        if data["kind"] not in ("deployment", "configuration", "rollback"):
            raise ValueError("Choose deployment, configuration or rollback.")
        timestamp = seconds(data["occurred_at"]) if isinstance(data["occurred_at"], str) else None
        if timestamp is None or not 0 <= timestamp <= self.clock() + 300:
            raise ValueError("Change timestamp must be valid UTC and no more than five minutes in the future.")
        from datetime import datetime, timezone
        occurred = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        with self.store.transaction():
            self.catalog.get(service_id)
            if self.store.db.execute("SELECT COUNT(*) FROM service_changes").fetchone()[0] >= 1000:
                raise ValueError("Change history limit: 1,000.")
            change_id = uuid.uuid4().hex[:12]
            self.store.db.execute("INSERT INTO service_changes VALUES(?,?,?,?,?,?,?,?,?)",
                                  (change_id, service_id, data["kind"], text(data["title"], 150, True), text(data["revision"], 100), occurred, note_text(data["note"]), text(actor, 100), now()))
        return self.get_change(change_id)

    def get_change(self, change_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM service_changes WHERE id=?", (text(change_id, 12, True),)).fetchone()
            if row is None:
                raise KeyError("Change not found.")
            service = self.catalog.get(row["service_id"])
            return dict(row) | {"service": service["name"], "environment": service["environment"]}

    def changes(self, env):
        with self.store.lock:
            rows = list(self.store.db.execute("SELECT c.id FROM service_changes c JOIN services s ON s.id=c.service_id WHERE s.environment=? ORDER BY c.occurred_at DESC LIMIT 1000", (env,)))
            return [self.get_change(r["id"]) for r in rows]

    def impact(self, change_id):
        with self.store.lock:
            item = self.get_change(change_id)
            timestamp, at = seconds(item["occurred_at"]), self.clock()
            records = self.records(item["service_id"])
            before = request_sample(records, timestamp - 1800, timestamp)
            after = request_sample(records, timestamp, min(at, timestamp + 1800))
            complete = at >= timestamp + 1800
            enough = min(before["total"], after["total"]) >= 20 and not before["unknown"] and not after["unknown"] and complete
            delta = (after["bad_fraction"] - before["bad_fraction"]) * 100 if enough else None
            return item | {"before": before, "after": after, "window_minutes": 30, "after_window_complete": complete,
                           "state": "comparable" if enough else "insufficient_data", "error_delta_percentage_points": delta,
                           "interpretation": "Temporal association only. Traffic mix, sampling, other changes and concurrent failures are uncontrolled."}

    def nearby_changes(self, incident):
        start, end = seconds(incident["first_seen"]), seconds(incident["last_seen"])
        if start is None or end is None:
            return []
        names = set(incident["services"]) | set(incident["anchor"])
        return [change for change in self.changes(incident["environment"]) if change["service"] in names and start - 1800 <= seconds(change["occurred_at"]) <= end + 1800][:50]
