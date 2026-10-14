"""Controlled HTTP monitoring with durable checks and deduplicated transitions."""

import http.client
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

from investigator.analysis import redact
from investigator.store import now


DEFAULTS = {"name": "", "url": "", "interval": 30, "expected_status": 200,
            "latency_limit": 1000, "failure_threshold": 3, "recovery_threshold": 2,
            "cooldown": 300, "enabled": False, "auto_incident": False}
LIMITS = {"interval": (5, 3600), "expected_status": (100, 599),
          "latency_limit": (1, 60000), "failure_threshold": (1, 20),
          "recovery_threshold": (1, 20), "cooldown": (0, 86400)}


def validate(config, policy=None):
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("Unknown monitor fields.")
    result = DEFAULTS | config
    if not isinstance(result["name"], str) or not 1 <= len(result["name"].strip()) <= 100:
        raise ValueError("Monitor name must contain 1-100 characters.")
    result["name"] = redact(result["name"].strip())
    for key, (low, high) in LIMITS.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    for key in ("enabled", "auto_incident"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    url = result["url"]
    if not isinstance(url, str) or len(url) > 1000 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError("Invalid monitor URL.")
    parsed = urlsplit(url)
    if parsed.scheme == "https" and policy is not None:
        policy.validate(url)
        if redact(url) != url:
            raise ValueError("Monitor URLs cannot contain secrets.")
        result["url"] = url
        return result
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in url or redact(url) != url):
        raise ValueError("Use http://127.0.0.1:PORT/path without credentials, query or fragment. Only loopback targets are allowed.")
    if not parsed.port or not 1 <= parsed.port <= 65535:
        raise ValueError("An explicit target port is required.")
    result["url"] = f"http://127.0.0.1:{parsed.port}{parsed.path or '/'}"
    return result


def probe(config, policy=None):
    """Loopback pinning or approved public TLS, without redirects or body capture."""
    parsed = urlsplit(config["url"])
    start = time.perf_counter()
    status, reason = None, "Connection failed"
    connection = None
    try:
        if parsed.scheme == "https":
            if policy is None:
                raise ValueError("Remote monitoring is disabled.")
            connection, path = policy.connection(config["url"])
        else:
            validate(config)
            connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
            path = parsed.path or "/"
        connection.request("GET", path, headers={"User-Agent": "Tracewise-Monitor/0.4"})
        response = connection.getresponse()
        status = response.status
        reason = "Expected status" if status == config["expected_status"] else f"Unexpected HTTP {status}"
    except ValueError:
        reason = "Target blocked by egress policy"
    except (OSError, http.client.HTTPException):
        reason = "Connection failed or timed out"
    finally:
        if connection:
            connection.close()
    latency = round((time.perf_counter() - start) * 1000, 2)
    if status == config["expected_status"] and latency > config["latency_limit"]:
        reason = "Latency limit exceeded"
    return {"timestamp": now(), "status": status, "latency": latency,
            "success": status == config["expected_status"] and latency <= config["latency_limit"], "reason": reason}


class MonitorEngine:
    def __init__(self, store, stream, probe_fn=probe, clock=time.time, policy=None, delivery=None, observer=None):
        self.store, self.stream, self.probe_fn, self.clock = store, stream, probe_fn, clock
        self.policy, self.delivery = policy, delivery
        self.observer, self.observer_error = observer, None
        if probe_fn is probe:
            self.probe_fn = lambda config: probe(config, policy)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS monitors(id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_checks(id INTEGER PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(id), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_alerts(id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(id), case_id TEXT REFERENCES cases(id), status TEXT NOT NULL, opened_at TEXT NOT NULL, recovered_at TEXT, message TEXT NOT NULL);
            """)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="tracewise-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()

    def _loop(self):
        while not self.stop_event.wait(1):
            try:
                for item in self.list():
                    if self.stop_event.is_set():
                        break
                    if item["config"]["enabled"] and item["state"]["next_check"] <= self.clock():
                        self.check(item["id"], scheduled=True)
                if self.delivery:
                    self.delivery.flush()
                self.last_error = None
            except Exception as exc:
                # Expose worker faults to the UI; never turn a failed worker into fake uptime.
                self.last_error = type(exc).__name__

    def list(self):
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM monitors ORDER BY rowid DESC").fetchall()
            return [{"id": r["id"], "config": json.loads(r["config"]), "state": json.loads(r["state"])} for r in rows]

    def get(self, monitor_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
            if row is None:
                raise KeyError("Monitor not found.")
            checks = self.store.db.execute("SELECT data FROM monitor_checks WHERE monitor_id=? ORDER BY id DESC LIMIT 200", (monitor_id,)).fetchall()
            items = [json.loads(c["data"]) for c in checks]
            success = sum(c["success"] for c in items)
            return {"id": row["id"], "config": json.loads(row["config"]), "state": json.loads(row["state"]),
                    "checks": items, "sample_count": len(items), "check_success_percent": round(success / len(items) * 100, 2) if items else None}

    def create(self, config, retain=False):
        if retain is not True:
            raise ValueError("Explicit retention consent is required for monitor settings, checks and automatic incidents.")
        config = validate(config, self.policy)
        with self.lock, self.store.lock, self.store.db:
            if len(self.list()) >= 20:
                raise ValueError("Limit: 20 local monitors.")
            monitor_id = uuid.uuid4().hex[:12]
            state = {"failures": 0, "successes": 0, "phase": "pending", "alert_id": None,
                     "last_opened": None, "next_check": self.clock(), "last": None}
            self.store.db.execute("INSERT INTO monitors VALUES(?,?,?)", (monitor_id, json.dumps(config), json.dumps(state)))
        return self.get(monitor_id)

    def update(self, monitor_id, changes):
        if not isinstance(changes, dict):
            raise ValueError("Changes must be an object.")
        with self.lock, self.store.lock, self.store.db:
            item = self.get(monitor_id)
            config = validate(item["config"] | changes, self.policy)
            if config["url"] != item["config"]["url"]:
                raise ValueError("Target URL is immutable; create a new monitor for a new target.")
            state = item["state"]
            state.update(failures=0, successes=0, next_check=self.clock())
            self.store.db.execute("UPDATE monitors SET config=?,state=? WHERE id=?", (json.dumps(config), json.dumps(state), monitor_id))
        return self.get(monitor_id)

    def alerts(self):
        with self.store.lock:
            return [dict(r) for r in self.store.db.execute("SELECT a.*,json_extract(m.config,'$.name') AS monitor_name FROM monitor_alerts a JOIN monitors m ON m.id=a.monitor_id ORDER BY a.rowid DESC LIMIT 200")]

    def acknowledge(self, alert_id):
        with self.lock, self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT status FROM monitor_alerts WHERE id=?", (alert_id,)).fetchone()
            if row is None:
                raise KeyError("Alert not found.")
            if row["status"] == "open":
                self.store.db.execute("UPDATE monitor_alerts SET status='acknowledged' WHERE id=?", (alert_id,))
        return {"acknowledged": True}

    def check(self, monitor_id, scheduled=False):
        # Serialize probes and transitions: manual checks cannot race scheduled checks.
        with self.lock:
            item = self.get(monitor_id)
            config, state = item["config"], item["state"]
            prior_alert = state["alert_id"]
            if scheduled and (not config["enabled"] or state["next_check"] > self.clock()):
                return item
            result = self.probe_fn(config)
            with self.store.lock, self.store.db:
                state["next_check"] = self.clock() + config["interval"]
                state["last"] = result
                state["failures"] = 0 if result["success"] else state["failures"] + 1
                state["successes"] = state["successes"] + 1 if result["success"] else 0
                self.store.db.execute("INSERT INTO monitor_checks(monitor_id,data) VALUES(?,?)", (monitor_id, json.dumps(result)))
                self.store.db.execute("DELETE FROM monitor_checks WHERE monitor_id=? AND id NOT IN (SELECT id FROM monitor_checks WHERE monitor_id=? ORDER BY id DESC LIMIT 200)", (monitor_id, monitor_id))
                event = {"timestamp": result["timestamp"], "level": "INFO" if result["success"] else "ERROR",
                         "service": config["name"], "message": f"HTTP monitor: {result['reason']}; latency={result['latency']}ms", "status": result["status"], "path": urlsplit(config["url"]).path}
                if state["failures"] >= config["failure_threshold"] and not state["alert_id"]:
                    if state["last_opened"] is None or self.clock() - state["last_opened"] >= config["cooldown"]:
                        case_id = None
                        if config["auto_incident"]:
                            # Existing case creation applies normalization and secret redaction.
                            checks = list(reversed(self.get(monitor_id)["checks"][:config["failure_threshold"]]))
                            evidence = [event | {"timestamp": c["timestamp"], "message": f"HTTP monitor: {c['reason']}; latency={c['latency']}ms", "status": c["status"]} for c in checks]
                            target = urlsplit(config["url"])
                            source = "Synthetic controlled HTTP monitor demo" if target.scheme == "http" and target.path == "/monitor-demo" else "Automatic HTTP monitor evidence"
                            case = self.store.create("\n".join(json.dumps(e) for e in evidence), "Monitor failure snapshot", f"Monitor: {config['name']}", "high", source)
                            case_id = case["id"]
                        alert_id = uuid.uuid4().hex[:12]
                        self.store.db.execute("INSERT INTO monitor_alerts VALUES(?,?,?,?,?,?,?)", (alert_id, monitor_id, case_id, "open", now(), None, result["reason"]))
                        state.update(alert_id=alert_id, last_opened=self.clock(), phase="failing")
                        if self.delivery:
                            self.delivery.enqueue(alert_id, "opened", config["name"], result["reason"])
                    else:
                        state["phase"] = "cooldown"
                elif state["alert_id"] and state["successes"] >= config["recovery_threshold"]:
                    if self.delivery:
                        self.delivery.enqueue(state["alert_id"], "recovered", config["name"], result["reason"])
                    row = self.store.db.execute("SELECT case_id FROM monitor_alerts WHERE id=?", (state["alert_id"],)).fetchone()
                    self.store.db.execute("UPDATE monitor_alerts SET status='recovered',recovered_at=? WHERE id=?", (now(), state["alert_id"]))
                    if row["case_id"]:
                        case = self.store.get(row["case_id"])
                        if case["status"] in {"open", "investigating"}:
                            self.store.update(case["id"], {"status": "monitoring"}, case["version"])
                            self.store.add_note(case["id"], "Monitor recovery threshold reached. Status moved to monitoring; root cause and final resolution require operator review.")
                    state.update(alert_id=None, phase="healthy")
                    self.store.db.execute("DELETE FROM monitor_alerts WHERE status='recovered' AND id NOT IN (SELECT id FROM monitor_alerts ORDER BY rowid DESC LIMIT 1000)")
                elif not state["alert_id"]:
                    state["phase"] = "healthy" if result["success"] else "degraded"
                self.store.db.execute("UPDATE monitors SET state=? WHERE id=?", (json.dumps(state), monitor_id))
            self.stream.ingest(json.dumps(event))
            if self.observer:
                try:
                    self.observer(monitor_id, config, result, state["alert_id"] or prior_alert, recovered=bool(prior_alert and not state["alert_id"]))
                    self.observer_error = None
                except Exception as exc:
                    self.observer_error = type(exc).__name__
            return self.get(monitor_id)
