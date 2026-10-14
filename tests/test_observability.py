import http.client
import json
import math
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import Mock

from investigator.analysis import normalize_record, parse_logs
from investigator.experiments import ExperimentManager, SCENARIOS, run_trial, percentile
from investigator.mesh import ServiceMesh, SERVICES
from investigator.observability import summarize, seconds
from investigator.store import CaseStore, ConflictError
from server import InvestigatorServer


def span(trace="a", service="orders", dependency="payments", timestamp="2026-10-14T00:00:00Z", status=503):
    return {"request_id": trace, "span_id": trace + service, "parent_span_id": "", "service": service,
            "dependency": dependency, "span_kind": "client", "timestamp": timestamp,
            "start_time": timestamp, "duration_ms": 12, "status": status, "message": "Dependency failed", "level": "ERROR"}


class SpanTests(unittest.TestCase):
    def test_parser_preserves_valid_span_metadata(self):
        record = normalize_record(span(), 1)
        self.assertEqual(record["dependency"], "payments")
        self.assertEqual(record["span_kind"], "client")
        self.assertEqual(record["duration_ms"], 12)
        self.assertIn("Z", record["start_time"])

    def test_parser_rejects_nonfinite_negative_and_bool_durations(self):
        for value in [math.nan, math.inf, -1, True, "12", 4000000, 10 ** 1000]:
            self.assertNotIn("duration_ms", normalize_record(span() | {"duration_ms": value}, 1))

    def test_span_strings_are_bounded_and_redacted(self):
        record = normalize_record(span() | {"span_id": "x" * 500, "dependency": "password=unsafe"}, 1)
        self.assertEqual(len(record["span_id"]), 150)
        self.assertNotIn("unsafe", record["dependency"])

    def test_legacy_null_status_is_supported(self):
        result = summarize([normalize_record({"request_id": "legacy", "message": "unknown", "level": "ERROR", "service": "unknown"}, 1)])
        self.assertEqual(result["traces"][0]["errors"], 1)
        self.assertEqual(result["episodes"][0]["suspected_dependencies"], [])

    def test_same_anchor_groups_requests_and_keeps_evidence(self):
        result = summarize([span("a"), span("b", timestamp="2026-10-14T00:00:02Z")])
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual(result["episodes"][0]["trace_ids"], ["a", "b"])
        self.assertEqual(result["episodes"][0]["evidence_ids"], ["S00001", "S00002"])

    def test_distinct_anchors_do_not_merge(self):
        result = summarize([span("a"), span("b", dependency="inventory")])
        self.assertEqual(len(result["episodes"]), 2)

    def test_time_window_uses_episode_start_not_infinite_chaining(self):
        result = summarize([span("a"), span("b", timestamp="2026-10-14T00:00:25Z"), span("c", timestamp="2026-10-14T00:00:50Z")])
        self.assertEqual(len(result["episodes"]), 2)

    def test_undated_errors_do_not_time_merge(self):
        result = summarize([span("a", timestamp=None), span("b", timestamp=None)])
        self.assertEqual(len(result["episodes"]), 2)

    def test_deepest_failure_is_hypothesis_and_first_observation_stays_explicit(self):
        result = summarize([span("a", "gateway", "orders"), span("a", "orders", "payments", timestamp="2026-10-14T00:00:01Z")])
        self.assertEqual(result["episodes"][0]["suspected_dependencies"], ["payments"])
        self.assertEqual(result["episodes"][0]["first_observed_failure"]["service"], "gateway")
        self.assertIn("not causal proof", " ".join(result["limitations"]))

    def test_missing_parent_count(self):
        result = summarize([span() | {"parent_span_id": "absent"}])
        self.assertEqual(result["traces"][0]["incomplete_parents"], 1)

    def test_topology_counts_only_observed_calls(self):
        result = summarize([span()])
        edge = next(e for e in result["edges"] if e["target"] == "payments")
        self.assertEqual((edge["calls"], edge["errors"]), (1, 1))
        self.assertEqual(next(e for e in result["edges"] if e["target"] == "inventory")["calls"], 0)

    def test_empty_and_naive_timestamps(self):
        self.assertEqual(summarize([])["traces"], [])
        self.assertEqual(seconds("2026-10-14T00:00:00"), seconds("2026-10-14T00:00:00Z"))


class MeshTests(unittest.TestCase):
    def test_four_actual_services_propagate_context(self):
        with tempfile.TemporaryDirectory() as directory:
            mesh = ServiceMesh(directory)
            try:
                connection = http.client.HTTPConnection("127.0.0.1", mesh.ports["gateway"], timeout=3)
                connection.request("GET", "/work", headers={"X-Request-ID": "abc123"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            finally:
                mesh.close()
            records = list(mesh.records)
            self.assertEqual({r["service"] for r in records}, set(SERVICES))
            self.assertEqual({r["request_id"] for r in records}, {"abc123"})
            self.assertEqual(len(records), 7)
            self.assertEqual(summarize(records)["traces"][0]["incomplete_parents"], 0)

    def test_real_fault_matrix(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                result = run_trial(19, scenario)
                self.assertEqual(len(result["samples"]), 12)
                self.assertEqual(len(result["analysis"]["traces"]), 12)
                if scenario == "baseline":
                    self.assertEqual(result["metrics"]["false_alerts"], 0)
                    self.assertIsNone(result["metrics"]["missed_incidents"])
                else:
                    self.assertEqual(result["metrics"]["missed_incidents"], 0)
                    self.assertEqual(result["metrics"]["duplicate_alerts"], 0)
                    self.assertIsNotNone(result["metrics"]["recovery_seconds"])
                    self.assertEqual(result["metrics"]["failed_checks"], 4)

    def test_cancel_trial_and_percentiles(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            run_trial(7, "baseline", stop)
        self.assertEqual(percentile([3, 1, 2], .5), 2)
        self.assertIsNone(percentile([], .5))


class ExperimentStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()
        self.runner = Mock(return_value={"id": "7-baseline", "records": [], "analysis": {"episodes": []}})
        self.manager = ExperimentManager(self.store, self.runner)

    def tearDown(self):
        self.manager.close()
        self.store.close()

    def test_retention_and_config_validation(self):
        with self.assertRaises(ValueError):
            self.manager.start({})
        for config in [{"url": "https://evil.example"}, {"seeds": [True]}, {"seeds": [1, 1]}, {"scenarios": ["unknown"]}, {"scenarios": []}, {"scenarios": [None]}, {"seeds": []}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.manager.start(config, True)

    def test_worker_complete_progress_and_configuration(self):
        item = self.manager.start({"seeds": [7, 19], "scenarios": ["baseline"]}, True)
        self.manager.thread.join()
        saved = self.manager.get(item["id"])
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["result"]["completed"], 2)
        self.assertEqual(self.runner.call_count, 2)

    def test_worker_error_is_visible_without_exception_secrets(self):
        self.runner.side_effect = RuntimeError("private details")
        item = self.manager.start({"seeds": [7], "scenarios": ["baseline"]}, True)
        self.manager.thread.join()
        saved = self.manager.get(item["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["result"]["error"], "RuntimeError")
        self.assertNotIn("private details", json.dumps(saved))

    def test_busy_cancel_and_cleanup(self):
        entered = threading.Event()
        def runner(seed, scenario, stop):
            entered.set()
            stop.wait(3)
            raise InterruptedError()
        self.manager.runner = runner
        item = self.manager.start({"seeds": [7], "scenarios": ["baseline"]}, True)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ConflictError):
            self.manager.start({}, True)
        self.manager.cancel()
        self.manager.thread.join()
        self.assertEqual(self.manager.get(item["id"])["status"], "cancelled")

    def test_restart_marks_incomplete_jobs_interrupted(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO experiments VALUES('stale','running','now','{}','{}')")
        restarted = ExperimentManager(self.store)
        self.assertEqual(restarted.get("stale")["status"], "interrupted")
        restarted.close()

    def test_save_correlated_case_is_idempotent_and_retains_span_fields(self):
        records = [span()]
        self.runner.return_value = {"id": "7-payments-timeout", "scenario": "payments-timeout", "seed": 7, "records": records, "analysis": summarize(records)}
        item = self.manager.start({"seeds": [7], "scenarios": ["payments-timeout"]}, True)
        self.manager.thread.join()
        first = self.manager.save_episode(item["id"], "7-payments-timeout", "C001")
        second = self.manager.save_episode(item["id"], "7-payments-timeout", "C001")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["report"]["records"][0]["dependency"], "payments")
        self.assertTrue(first["source"].startswith("Synthetic"))

    def test_missing_experiment(self):
        with self.assertRaises(KeyError):
            self.manager.get("absent")


class ObservationHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = InvestigatorServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        response = conn.getresponse()
        status, headers = response.status, dict(response.getheaders())
        raw = response.read()
        data = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
        conn.close()
        return status, headers, data

    def test_trace_analysis_and_redaction(self):
        status, _, result = self.request("POST", "/api/traces/analyze", {"content": json.dumps([span() | {"message": "password=private"}])})
        self.assertEqual(status, 200)
        self.assertNotIn("private", json.dumps(result))

    def test_experiment_list_and_missing(self):
        self.assertEqual(self.request("GET", "/api/experiments")[0], 200)
        self.assertEqual(self.request("GET", "/api/experiments/missing")[0], 404)

    def test_creation_requires_consent(self):
        self.assertEqual(self.request("POST", "/api/experiments", {"config": {}})[0], 400)

    def test_export_endpoint(self):
        self.server.experiments.runner = Mock(return_value={"id": "7-baseline", "records": [], "analysis": {"episodes": []}})
        status, _, result = self.request("POST", "/api/experiments", {"config": {"seeds": [7], "scenarios": ["baseline"]}, "retain": True})
        self.assertEqual(status, 200)
        self.server.experiments.thread.join()
        status, headers, _ = self.request("GET", "/api/experiments/" + result["id"] + "/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        status, headers, report = self.request("GET", "/api/experiments/" + result["id"] + "/report")
        self.assertEqual(status, 200)
        self.assertIn("Fault Report", report)
        self.assertIn("text/markdown", headers["Content-Type"])

    def test_correlated_case_http_retention(self):
        records = [span()]
        self.server.experiments.runner = Mock(return_value={"id": "7-payments-timeout", "seed": 7, "scenario": "payments-timeout", "records": records, "analysis": summarize(records)})
        _, _, result = self.request("POST", "/api/experiments", {"config": {"seeds": [7], "scenarios": ["payments-timeout"]}, "retain": True})
        self.server.experiments.thread.join()
        endpoint = "/api/experiments/" + result["id"] + "/cases"
        body = {"run_id": "7-payments-timeout", "episode_id": "C001"}
        self.assertEqual(self.request("POST", endpoint, body)[0], 400)
        self.assertEqual(self.request("POST", endpoint, body | {"retain": True})[0], 200)
