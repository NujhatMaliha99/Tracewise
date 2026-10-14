"""Isolated, persisted multi-seed HTTP fault experiments with measured outcomes."""

import json
import random
import tempfile
import threading
import time
import uuid
from contextlib import closing

from investigator.mesh import ServiceMesh
from investigator.monitoring import MonitorEngine
from investigator.observability import summarize
from investigator.store import CaseStore, ConflictError, now
from investigator.stream import LiveBuffer

SCENARIOS = ("baseline", "payments-timeout", "service-down", "database-contention")


def markdown_report(experiment):
    result = experiment["result"]
    lines = ["# Tracewise Multi-Service Fault Report", "", f"Experiment: {experiment['id']} / {experiment['status']}",
             f"Created UTC: {experiment['created_at']}", "", result.get("source", "Controlled local experiment"), "",
             result.get("protocol", ""), "", "| Seed | Scenario | Detection s | Recovery s | Missed | Duplicate | False alerts | P95 header ms |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for run in result.get("runs", []):
        metrics = run.get("metrics", {})
        values = [metrics.get(key) for key in ("detection_seconds", "recovery_seconds", "missed_incidents", "duplicate_alerts", "false_alerts", "p95_latency_ms")]
        lines.append("| " + " | ".join(str(v) if v is not None else "N/A" for v in [run.get("seed"), run.get("scenario"), *values]) + " |")
    lines += ["", "## Boundaries", "", result.get("limits", ""), "",
              "- Seeded fault placement is repeatable; actual latency is OS/network dependent.",
              "- P95 uses the floor-index empirical sample quantile over 12 header-latency observations.",
              "- Correlation is a hypothesis, not a verified root cause. Inspect linked span evidence in the JSON export.",
              "- No production dataset, production detection guarantee or autonomous repair is claimed.",
              "- User monitors, SMTP credentials and external notification delivery are not part of these isolated runs."]
    return "\n".join(lines) + "\n"


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3) if ordered else None


def run_trial(seed, scenario, stop=None):
    stop = stop or threading.Event()
    rng = random.Random(seed)
    fault_index = 3 + rng.randrange(2)
    recovery_index = fault_index + 4
    observations, detection, recovery = [], None, None
    injected, restored = None, None
    with tempfile.TemporaryDirectory(prefix="tracewise-mesh-") as directory, closing(CaseStore()) as store:
        mesh = ServiceMesh(directory)
        try:
            engine = MonitorEngine(store, LiveBuffer())
            item = engine.create({"name": "Isolated mesh experiment", "url": f"http://127.0.0.1:{mesh.ports['gateway']}/work",
                                  "failure_threshold": 2, "recovery_threshold": 2, "cooldown": 0}, True)
            for index in range(12):
                if stop.is_set():
                    raise InterruptedError("Experiment cancelled")
                if scenario != "baseline" and index == fault_index:
                    mesh.fault(scenario)
                    injected = time.perf_counter()
                if scenario != "baseline" and index == recovery_index:
                    mesh.recover()
                    restored = time.perf_counter()
                result = engine.check(item["id"])
                elapsed = time.perf_counter()
                observations.append(result["state"]["last"] | {"index": index, "phase": result["state"]["phase"]})
                if injected is not None and detection is None and result["state"]["alert_id"]:
                    detection = round(elapsed - injected, 4)
                if restored is not None and recovery is None and any(a["status"] == "recovered" for a in engine.alerts()):
                    recovery = round(elapsed - restored, 4)
                time.sleep(rng.uniform(.003, .008))
            alerts = engine.alerts()
        finally:
            mesh.close()
        records = list(mesh.records)
    latencies = [item["latency"] for item in observations]
    return {"id": f"{seed}-{scenario}", "seed": seed, "scenario": scenario, "samples": observations,
            "fault_check": fault_index if scenario != "baseline" else None,
            "recovery_check": recovery_index if scenario != "baseline" else None,
            "metrics": {"detection_seconds": detection, "recovery_seconds": recovery,
                        "missed_incidents": int(detection is None) if scenario != "baseline" else None,
                        "duplicate_alerts": max(0, len(alerts) - 1) if scenario != "baseline" else None,
                        "false_alerts": len(alerts) if scenario == "baseline" else None,
                        "p50_latency_ms": percentile(latencies, .5), "p95_latency_ms": percentile(latencies, .95),
                        "failed_checks": sum(not item["success"] for item in observations)},
            "records": records, "analysis": summarize(records), "alert_count": len(alerts)}


class ExperimentManager:
    def __init__(self, store, runner=run_trial):
        self.store, self.runner = store, runner
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, config TEXT NOT NULL, result TEXT NOT NULL)")
            store.db.execute("UPDATE experiments SET status='interrupted' WHERE status IN ('queued','running')")

    def list(self):
        with self.store.lock:
            return [{"id": r["id"], "status": r["status"], "created_at": r["created_at"], "config": json.loads(r["config"]),
                     "completed": json.loads(r["result"]).get("completed", 0)} for r in self.store.db.execute("SELECT * FROM experiments ORDER BY rowid DESC LIMIT 50")]

    def get(self, experiment_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
            if row is None:
                raise KeyError("Experiment not found")
            return {"id": row["id"], "status": row["status"], "created_at": row["created_at"], "config": json.loads(row["config"]), "result": json.loads(row["result"])}

    def start(self, config, retain=False):
        if retain is not True:
            raise ValueError("Explicit experiment evidence retention consent is required.")
        if not isinstance(config, dict) or set(config) - {"seeds", "scenarios"}:
            raise ValueError("Expected seeds and scenarios only.")
        seeds, scenarios = config.get("seeds", [7, 19, 42]), config.get("scenarios", list(SCENARIOS))
        if not isinstance(seeds, list) or not 1 <= len(seeds) <= 5 or any(type(s) is not int or not 0 <= s <= 999999 for s in seeds) or len(set(seeds)) != len(seeds):
            raise ValueError("Use 1-5 unique integer seeds between 0 and 999999.")
        if not isinstance(scenarios, list) or not scenarios or any(not isinstance(s, str) or s not in SCENARIOS for s in scenarios) or len(set(scenarios)) != len(scenarios):
            raise ValueError("Choose distinct supported scenarios.")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ConflictError("An experiment is already running.")
            self.stop_event.clear()
            experiment_id = uuid.uuid4().hex[:12]
            config = {"seeds": seeds, "scenarios": scenarios}
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO experiments VALUES(?,?,?,?,?)", (experiment_id, "queued", now(), json.dumps(config), json.dumps({"completed": 0, "runs": []})))
                self.store.db.execute("DELETE FROM experiments WHERE id NOT IN (SELECT id FROM experiments ORDER BY rowid DESC LIMIT 50)")
            self.thread = threading.Thread(target=self._run, args=(experiment_id, config), name="tracewise-experiments", daemon=True)
            self.thread.start()
        return self.get(experiment_id)

    def _run(self, experiment_id, config):
        result = {"completed": 0, "runs": [], "source": "Controlled local multi-service experiments / actual HTTP and SQLite faults",
                  "protocol": "12 sequential manual probe checks per run; threshold 2/2, no scheduler, isolated from user monitors and SMTP.",
                  "limits": "Seeds repeat fault placement and request spacing, not OS/network timing. Detection seconds are for this accelerated protocol, not production polling intervals."}
        self._save(experiment_id, "running", result)
        try:
            for seed in config["seeds"]:
                for scenario in config["scenarios"]:
                    if self.stop_event.is_set():
                        raise InterruptedError()
                    result["runs"].append(self.runner(seed, scenario, self.stop_event))
                    result["completed"] += 1
                    self._save(experiment_id, "running", result)
            self._save(experiment_id, "complete", result)
        except InterruptedError:
            self._save(experiment_id, "cancelled", result)
        except Exception as exc:
            result["error"] = type(exc).__name__
            self._save(experiment_id, "failed", result)

    def _save(self, experiment_id, status, result):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE experiments SET status=?,result=? WHERE id=?", (status, json.dumps(result), experiment_id))

    def cancel(self):
        self.stop_event.set()
        return {"cancellation_requested": True}

    def save_episode(self, experiment_id, run_id, episode_id):
        experiment = self.get(experiment_id)
        run = next((r for r in experiment["result"].get("runs", []) if r["id"] == run_id), None)
        if run is None:
            raise KeyError("Run not found")
        episode = next((e for e in run["analysis"]["episodes"] if e["id"] == episode_id), None)
        if episode is None:
            raise KeyError("Episode not found")
        records = [r for r in run["records"] if r["request_id"] in episode["trace_ids"]]
        return self.store.create(json.dumps(records), "Correlated mesh span evidence", f"Mesh / {run['scenario']} / seed {run['seed']} / {episode_id}",
                                 "high", "Synthetic controlled multi-service fault experiment; correlation is not causal proof",
                                 f"experiment:{experiment_id}:{run_id}:{episode_id}")

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
