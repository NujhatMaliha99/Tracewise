import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from investigator.monitoring import MonitorEngine, probe, validate
from investigator.store import CaseStore, now
from investigator.stream import LiveBuffer


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()
        self.stream = LiveBuffer()
        self.time = 1000
        self.healthy = False
        self.calls = 0
        self.engine = MonitorEngine(self.store, self.stream, self.fake_probe, lambda: self.time)

    def tearDown(self):
        self.engine.stop()
        self.store.close()

    def fake_probe(self, config):
        self.calls += 1
        return {"timestamp": now(), "status": 200 if self.healthy else 503, "latency": 10,
                "success": self.healthy, "reason": "Expected status" if self.healthy else "Unexpected HTTP 503"}

    def create(self, **changes):
        return self.engine.create({"name": "test-api", "url": "http://127.0.0.1:8000/health", "failure_threshold": 2, "auto_incident": True} | changes, True)

    def test_retention_requires_consent(self):
        with self.assertRaises(ValueError):
            self.engine.create({"name": "test", "url": "http://127.0.0.1:8000/"})

    def test_url_restrictions(self):
        for url in ["https://example.com", "http://192.168.1.1:80/", "http://169.254.169.254:80/", "http://127.0.0.1:80/?token=secret", "http://user:pass@127.0.0.1:80/", "http://[::1]:80/", "http://127.0.0.1:80/#x", "file:///etc/passwd", "http://127.0.0.1:99999/", "http://127.0.0.1:80/\r\nx"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate({"name": "bad", "url": url})

    def test_localhost_is_pinned(self):
        self.assertEqual(validate({"name": "good", "url": "http://localhost:8000/health"})["url"], "http://127.0.0.1:8000/health")

    def test_invalid_settings(self):
        for change in [{"interval": 1}, {"interval": True}, {"enabled": "yes"}, {"failure_threshold": 0}, {"extra": 1}, {"name": ""}, {"latency_limit": -1}, {"cooldown": -5}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.create(**change)

    def test_threshold_and_deduplication(self):
        item = self.create()
        self.engine.check(item["id"])
        self.assertEqual(len(self.engine.alerts()), 0)
        self.engine.check(item["id"])
        self.engine.check(item["id"])
        self.assertEqual(len(self.engine.alerts()), 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.list()[0]["summary"]["records"], 2)

    def test_recovery_needs_consecutive_successes(self):
        item = self.create(failure_threshold=1)
        self.engine.check(item["id"])
        self.healthy = True
        self.engine.check(item["id"])
        self.assertEqual(self.engine.alerts()[0]["status"], "open")
        self.engine.check(item["id"])
        self.assertEqual(self.engine.alerts()[0]["status"], "recovered")
        self.assertEqual(self.store.list()[0]["status"], "monitoring")
        self.assertEqual(len(self.store.get(self.store.list()[0]["id"])["notes"]), 1)

    def test_acknowledge_does_not_resolve(self):
        item = self.create(failure_threshold=1)
        self.engine.check(item["id"])
        self.engine.acknowledge(self.engine.alerts()[0]["id"])
        self.engine.check(item["id"])
        self.assertEqual(len(self.engine.alerts()), 1)
        self.assertEqual(self.engine.alerts()[0]["status"], "acknowledged")
        self.assertEqual(self.store.list()[0]["status"], "open")

    def test_manual_resolution_preserved(self):
        item = self.create(failure_threshold=1, recovery_threshold=1)
        self.engine.check(item["id"])
        case = self.store.list()[0]
        self.store.update(case["id"], {"status": "resolved"}, case["version"])
        self.healthy = True
        self.engine.check(item["id"])
        self.assertEqual(self.store.get(case["id"])["status"], "resolved")

    def test_cooldown_then_new_outage(self):
        item = self.create(failure_threshold=1, recovery_threshold=1)
        self.engine.check(item["id"])
        self.healthy = True
        self.engine.check(item["id"])
        self.healthy = False
        self.assertEqual(self.engine.check(item["id"])["state"]["phase"], "cooldown")
        self.assertEqual(len(self.engine.alerts()), 1)
        self.time += 301
        self.engine.check(item["id"])
        self.assertEqual(len(self.engine.alerts()), 2)

    def test_disabled_schedule_and_manual_check(self):
        item = self.create()
        self.engine.check(item["id"], scheduled=True)
        self.assertEqual(self.calls, 0)
        self.engine.check(item["id"])
        self.assertEqual(self.calls, 1)

    def test_schedule_not_due(self):
        item = self.create(enabled=True)
        self.engine.check(item["id"], scheduled=True)
        self.engine.check(item["id"], scheduled=True)
        self.assertEqual(self.calls, 1)
        self.time += 31
        self.engine.check(item["id"], scheduled=True)
        self.assertEqual(self.calls, 2)

    def test_checks_bounded(self):
        self.healthy = True
        item = self.create()
        for _ in range(205):
            self.engine.check(item["id"])
        result = self.engine.get(item["id"])
        self.assertEqual(result["sample_count"], 200)
        self.assertEqual(result["check_success_percent"], 100)

    def test_optional_incident_creation(self):
        item = self.create(auto_incident=False, failure_threshold=1)
        self.engine.check(item["id"])
        self.assertIsNone(self.engine.alerts()[0]["case_id"])
        self.assertEqual(self.store.list(), [])

    def test_failed_automatic_case_rolls_back_transition(self):
        item = self.create(failure_threshold=1)
        with patch.object(self.store, "_audit", side_effect=ValueError("Injected persistence fault")):
            with self.assertRaises(ValueError):
                self.engine.check(item["id"])
        self.assertEqual(self.engine.get(item["id"])["sample_count"], 0)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.engine.alerts(), [])
        self.assertEqual(self.engine.get(item["id"])["state"]["failures"], 0)

    def test_update_and_immutable_target(self):
        item = self.create()
        self.assertTrue(self.engine.update(item["id"], {"enabled": True})["config"]["enabled"])
        with self.assertRaises(ValueError):
            self.engine.update(item["id"], {"url": "http://127.0.0.1:9000/"})

    def test_unknown_monitor(self):
        with self.assertRaises(KeyError):
            self.engine.get("missing")

    def test_monitor_limit(self):
        for _ in range(20):
            self.create()
        with self.assertRaises(ValueError):
            self.create()

    def test_background_scheduler(self):
        observed = threading.Event()
        self.create(enabled=True)
        original = self.engine.probe_fn

        def observed_probe(config):
            result = original(config)
            observed.set()
            return result

        self.engine.probe_fn = observed_probe
        self.engine.start()
        self.assertTrue(observed.wait(3), "Background worker did not run due monitor")
        self.engine.stop()
        self.assertEqual(self.calls, 1)

    def test_concurrent_checks_deduplicate(self):
        item = self.create(failure_threshold=1)
        threads = [threading.Thread(target=self.engine.check, args=(item["id"],)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.engine.alerts()), 1)
        self.assertEqual(self.engine.get(item["id"])["sample_count"], 4)

    def test_persistent_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.sqlite3"
            store = CaseStore(path)
            engine = MonitorEngine(store, LiveBuffer(), self.fake_probe, lambda: self.time)
            item = engine.create({"name": "persist", "url": "http://127.0.0.1:8000/", "failure_threshold": 1}, True)
            engine.check(item["id"])
            store.close()
            store = CaseStore(path)
            try:
                restored = MonitorEngine(store, LiveBuffer())
                self.assertEqual(restored.get(item["id"])["sample_count"], 1)
                self.assertEqual(len(restored.alerts()), 1)
            finally:
                store.close()


class ProbeTests(unittest.TestCase):
    def test_real_http_and_no_redirect(self):
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/slow":
                    time.sleep(.05)
                self.send_response(302 if self.path == "/redirect" else 200)
                self.send_header("Location", "http://192.168.1.1/")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = validate({"name": "actual", "url": f"http://127.0.0.1:{server.server_port}/"})
            self.assertTrue(probe(config)["success"])
            self.assertEqual(probe(config | {"url": config["url"] + "redirect"})["status"], 302)
            self.assertFalse(probe(config | {"expected_status": 201})["success"])
            self.assertEqual(probe(config | {"url": config["url"] + "slow", "latency_limit": 1})["reason"], "Latency limit exceeded")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
