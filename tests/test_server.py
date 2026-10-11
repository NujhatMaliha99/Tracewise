import json
import logging
import threading
import unittest
import urllib.error
import urllib.request

from server import InvestigatorServer
from sdk.tracewise import TracewiseClient, TracewiseHandler


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = InvestigatorServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers or ({"Content-Type": "application/json"} if data else {}))
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def test_health(self):
        status, _, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_static(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"API Failure Investigator", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_traversal(self):
        self.assertEqual(self.request("/../server.py")[0], 404)

    def test_sample(self):
        status, _, body = self.request("/api/sample", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["summary"]["errors"], 7)

    def test_demo(self):
        status, _, body = self.request("/api/demo", {})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["summary"]["records"], 30)
        self.assertEqual(result["summary"]["errors"], 10)
        self.assertEqual(len(result["groups"]), 4)
        self.assertEqual(len({r["request_id"] for r in result["records"]}), 15)

    def test_live_ingestion_http(self):
        self.request("/api/live/clear", {})
        content = json.dumps({"level": "ERROR", "service": "api", "message": "timeout"})
        status, _, body = self.request("/api/ingest", {"content": content})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["accepted"], 1)
        status, _, body = self.request("/api/live")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["count"], 1)

    def test_live_clear_http(self):
        self.request("/api/demo", {})
        self.assertEqual(self.request("/api/live/clear", {})[0], 200)
        self.assertEqual(json.loads(self.request("/api/live")[2])["count"], 0)

    def test_invalid_ingestion_http(self):
        self.assertEqual(self.request("/api/ingest", {})[0], 400)

    def test_monitor_lifecycle_http(self):
        config = {"name": "Controlled HTTP monitor", "url": self.base + "/monitor-demo", "failure_threshold": 1, "recovery_threshold": 1, "auto_incident": True}
        status, _, body = self.request("/api/monitors", {"config": config, "retain": True})
        self.assertEqual(status, 200)
        monitor_id = json.loads(body)["id"]
        self.request("/api/monitor-demo", {"mode": "failure"})
        checked = self.request(f"/api/monitors/{monitor_id}/check", {})
        self.assertEqual(json.loads(checked[2])["state"]["phase"], "failing")
        alerts = json.loads(self.request("/api/alerts")[2])["alerts"]
        alert = next(a for a in alerts if a["monitor_id"] == monitor_id)
        self.assertIsNotNone(alert["case_id"])
        self.assertEqual(self.request(f"/api/alerts/{alert['id']}/acknowledge", {})[0], 200)
        self.request("/api/monitor-demo", {"mode": "healthy"})
        self.request(f"/api/monitors/{monitor_id}/check", {})
        self.assertEqual(json.loads(self.request(f"/api/cases/{alert['case_id']}")[2])["status"], "monitoring")

    def test_monitor_requires_consent_http(self):
        self.assertEqual(self.request("/api/monitors", {"config": {"name": "bad", "url": self.base}})[0], 400)

    def test_remote_monitor_rejected_http(self):
        self.assertEqual(self.request("/api/monitors", {"config": {"name": "bad", "url": "http://169.254.169.254:80/"}, "retain": True})[0], 400)

    def test_python_sdk_delivery(self):
        self.request("/api/live/clear", {})
        client = TracewiseClient("python-sdk", self.base)
        self.assertTrue(client.event("Authorization: Bearer private-token", "ERROR", request_id="sdk-test"))
        content = self.request("/api/live")[2].decode()
        self.assertIn("python-sdk", content)
        self.assertNotIn("private-token", content)
        original = RuntimeError("Controlled SDK exception")
        with self.assertRaises(RuntimeError) as caught:
            with client.capture():
                raise original
        self.assertIs(caught.exception, original)
        self.assertFalse(client.event("x" * 70000))
        self.assertEqual(client.dropped, 1)

    def test_sdk_remote_origin_rejected(self):
        with self.assertRaises(ValueError):
            TracewiseClient("api", "https://example.com")

    def test_sdk_logging_formatter_failure_is_isolated(self):
        client = TracewiseClient("api", self.base)
        record = logging.LogRecord("test", logging.ERROR, "test.py", 1, "%d", ("not-a-number",), None)
        TracewiseHandler(client).emit(record)
        self.assertEqual(client.dropped, 1)
        self.assertEqual(client.last_error, "TypeError")

    def test_direct_demo_blocked(self):
        self.assertEqual(self.request("/demo/checkout")[0], 403)

    def test_cross_origin_rejected(self):
        self.assertEqual(self.request("/api/sample", {}, {"Content-Type": "application/json", "Origin": "https://example.com"})[0], 403)

    def test_bad_host_rejected(self):
        self.assertEqual(self.request("/api/sample", {}, {"Content-Type": "application/json", "Host": "attacker.example"})[0], 403)

    def test_bad_content_type(self):
        self.assertEqual(self.request("/api/sample", {}, {"Content-Type": "text/plain"})[0], 415)

    def test_missing_content(self):
        self.assertEqual(self.request("/api/analyze", {})[0], 400)

    def test_unknown_post(self):
        self.assertEqual(self.request("/api/unknown", {})[0], 404)

    def test_download(self):
        status, headers, _ = self.request("/api/sample")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])

    def make_case(self):
        content = json.dumps({"level": "ERROR", "service": "api", "message": "Payment timed out", "timestamp": "2026-10-12T09:00:00Z"})
        status, _, body = self.request("/api/cases", {"content": content, "title": "HTTP test fixture"})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_cases_http(self):
        case = self.make_case()
        status, _, body = self.request("/api/cases/" + case["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["title"], "HTTP test fixture")

    def test_case_update_and_conflict_http(self):
        case = self.make_case()
        payload = {"version": 1, "changes": {"status": "monitoring"}}
        self.assertEqual(self.request("/api/cases/" + case["id"], payload)[0], 200)
        self.assertEqual(self.request("/api/cases/" + case["id"], payload)[0], 409)

    def test_case_export_http(self):
        case = self.make_case()
        status, headers, body = self.request(f"/api/cases/{case['id']}/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(json.loads(body)["id"], case["id"])

    def test_case_report_http(self):
        case = self.make_case()
        status, headers, body = self.request(f"/api/cases/{case['id']}/report")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertIn(b"Dependency request timeout", body)

    def test_case_note_http(self):
        case = self.make_case()
        status, _, body = self.request(f"/api/cases/{case['id']}/notes", {"body": "Checked dependency logs"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["notes"][0]["body"], "Checked dependency logs")

    def test_analytics_http(self):
        self.make_case()
        status, _, body = self.request("/api/analytics")
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(body)["cases"], 0)

    def test_missing_case_http(self):
        self.assertEqual(self.request("/api/cases/not-real")[0], 404)

    def test_seed_workspace_http(self):
        first = self.request("/api/workspace/seed", {})
        second = self.request("/api/workspace/seed", {})
        self.assertEqual(first[0], 200)
        self.assertEqual(json.loads(first[2])["ids"], json.loads(second[2])["ids"])

    def test_same_case_comparison_rejected(self):
        case = self.make_case()
        self.assertEqual(self.request("/api/compare", {"left": case["id"], "right": case["id"]})[0], 400)

    def test_runbooks_http(self):
        status, _, body = self.request("/api/runbooks")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["runbooks"]), 5)

    def test_mutation_requires_local_host(self):
        self.assertEqual(self.request("/api/cases", {}, {"Content-Type": "application/json", "Host": "evil.example"})[0], 403)


if __name__ == "__main__":
    unittest.main()
