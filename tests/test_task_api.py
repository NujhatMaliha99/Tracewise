import http.client
import json
import tempfile
import threading
import unittest
from contextlib import closing

from examples.task_api import TaskServer
from investigator.monitoring import MonitorEngine, probe
from investigator.store import CaseStore
from investigator.stream import LiveBuffer


class TaskAPITests(unittest.TestCase):
    def setUp(self):
        self.server = TaskServer(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"} | (headers or {}))
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def test_crud(self):
        status, task = self.request("POST", "/tasks", {"title": "Complete API integration"})
        self.assertEqual(status, 201)
        self.assertEqual(task["status"], "todo")
        path = "/tasks/" + task["id"]
        self.assertEqual(self.request("GET", path)[1]["title"], task["title"])
        self.assertEqual(len(self.request("GET", "/tasks")[1]["tasks"]), 1)
        self.assertEqual(self.request("PATCH", path, {"status": "done"})[1]["status"], "done")
        self.assertEqual(self.request("DELETE", path)[0], 200)
        self.assertEqual(self.request("GET", path)[0], 404)

    def test_health_checks_database(self):
        self.assertEqual(self.request("GET", "/health")[1]["database"], "ready")
        with self.server.lock, self.server.db:
            self.server.db.execute("DROP TABLE tasks")
        self.assertEqual(self.request("GET", "/health")[0], 503)

    def test_input_validation(self):
        for body in [{}, {"title": ""}, {"title": "x" * 201}, {"title": "x", "status": []}, {"title": "x", "owner": "user"}, []]:
            self.assertEqual(self.request("POST", "/tasks", body)[0], 400)

    def test_origin_host_and_content_type(self):
        self.assertEqual(self.request("POST", "/tasks", {"title": "x"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/health", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/tasks", {"title": "x"}, {"Content-Type": "text/plain"})[0], 415)

    def test_unknown_missing_and_oversized(self):
        self.assertEqual(self.request("PATCH", "/tasks/missing", {"status": "done"})[0], 404)
        self.assertEqual(self.request("GET", "/unknown")[0], 404)
        self.assertEqual(self.request("GET", "/tasks?unexpected=1")[0], 404)
        self.assertEqual(self.request("POST", "/tasks", {"title": "x" * 5000})[0], 413)

    def test_real_monitor_probe(self):
        result = probe({"name": "Task API", "url": f"http://127.0.0.1:{self.server.server_port}/health", "expected_status": 200, "latency_limit": 1000})
        self.assertTrue(result["success"])

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/tasks.sqlite3"
            server = TaskServer(("127.0.0.1", 0), path)
            server.db.execute("INSERT INTO tasks VALUES('persist','Persistent task','todo','now','now')")
            server.db.commit()
            server.server_close()
            server = TaskServer(("127.0.0.1", 0), path)
            self.assertEqual(server.db.execute("SELECT title FROM tasks WHERE id='persist'").fetchone()[0], "Persistent task")
            server.server_close()

    def test_actual_service_stop_restart_recovers_monitor(self):
        service = TaskServer(("127.0.0.1", 0))
        port = service.server_port
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        with closing(CaseStore()) as store:
            engine = MonitorEngine(store, LiveBuffer())
            item = engine.create({"name": "Task API test", "url": f"http://127.0.0.1:{port}/health", "failure_threshold": 1, "recovery_threshold": 1}, True)
            self.assertEqual(engine.check(item["id"])["state"]["phase"], "healthy")
            service.shutdown()
            service.server_close()
            thread.join()
            self.assertEqual(engine.check(item["id"])["state"]["phase"], "failing")
            service = TaskServer(("127.0.0.1", port))
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(engine.check(item["id"])["state"]["phase"], "healthy")
                self.assertEqual(engine.alerts()[0]["status"], "recovered")
            finally:
                service.shutdown()
                service.server_close()
                thread.join()
