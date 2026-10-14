import io
import json
import unittest

from investigator.access import password_hash
from task_app.app import TaskApplication, origin, telemetry


class TaskApplicationTests(unittest.TestCase):
    def setUp(self):
        self.app = TaskApplication()
        self.addCleanup(lambda: self.app.close())

    def request(self, method="GET", path="/api/tasks", data=None, cookie="", host="localhost:8801", origin_value=None):
        raw = json.dumps(data).encode() if data is not None else b""
        path, _, query = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
               "HTTP_HOST": host, "SERVER_PORT": "8801", "REMOTE_ADDR": "127.0.0.1",
               "HTTP_COOKIE": cookie, "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        if origin_value:
            env["HTTP_ORIGIN"] = origin_value
        response = {}
        def start(status, headers):
            response.update(code=int(status.split()[0]), headers=dict(headers))
        body = b"".join(self.app(env, start))
        return response["code"], json.loads(body), response["headers"]

    def create(self):
        code, task, _ = self.request("POST", data={"title": "Private task", "description": "Private notes"})
        self.assertEqual(code, 201)
        return task

    def test_empty_workspace(self):
        self.assertEqual(self.request()[1], {"tasks": []})

    def test_crud_archive_restore_history(self):
        task = self.create()
        path = "/api/tasks/" + task["id"]
        for changes in ({"status": "done"}, {"archived": True}, {"archived": False}):
            code, task, _ = self.request("PATCH", path, {"changes": changes, "version": task["version"]})
            self.assertEqual(code, 200)
        self.assertEqual(task["version"], 4)
        self.assertEqual(len(self.request(path=path + "/history")[1]["history"]), 4)
        self.assertEqual(len(self.request()[1]["tasks"]), 1)
        self.assertEqual(self.request(path="/api/tasks?archived=1")[1]["tasks"], [])

    def test_stale_version(self):
        task = self.create()
        self.assertEqual(self.request("PATCH", "/api/tasks/" + task["id"], {"changes": {"status": "done"}, "version": 0})[0], 409)

    def test_owner_isolation(self):
        task = self.app.store.create("alice", {"title": "Private"})
        self.assertEqual(self.app.store.list("bob"), [])
        with self.assertRaises(KeyError):
            self.app.store.get("bob", task["id"])
        with self.assertRaises(KeyError):
            self.app.store.update("bob", task["id"], {"status": "done"}, 1)
        with self.assertRaises(KeyError):
            self.app.store.history("bob", task["id"])

    def test_validation(self):
        for data in ({}, {"title": ""}, {"title": "x", "status": "other"}, {"title": "x", "due_date": "2026-02-30"}, {"title": "x", "archived": 1}, {"title": "x", "owner": "alice"}):
            with self.subTest(data=data):
                self.assertEqual(self.request("POST", data=data)[0], 400)

    def test_host_and_csrf(self):
        self.assertEqual(self.request(host="evil.example")[0], 403)
        self.assertEqual(self.request("POST", data={"title": "x"}, origin_value="https://evil.example")[0], 403)

    def test_health_and_headers(self):
        code, data, headers = self.request(path="/health")
        self.assertEqual(code, 200)
        self.assertEqual(data["database"], "ready")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_public_requires_auth(self):
        with self.assertRaises(ValueError):
            TaskApplication(public_origin="https://tasks.example.com")

    def test_public_login_viewer_logout(self):
        self.app.close()
        self.app = TaskApplication(accounts={"reader": {"role": "viewer", "hash": password_hash("test-password-123")}}, public_origin="https://tasks.example.com")
        self.assertEqual(self.request()[0], 401)
        code, _, headers = self.request("POST", "/api/login", {"username": "reader", "password": "test-password-123"})
        self.assertEqual(code, 200)
        self.assertIn("Secure", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.request(cookie=cookie)[0], 200)
        self.assertEqual(self.request("POST", data={"title": "x"}, cookie=cookie)[0], 403)
        self.assertEqual(self.request("POST", "/api/logout", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.request(cookie=cookie)[0], 401)

    def test_database_failure(self):
        self.app.store.db.execute("DROP TABLE task_items")
        self.assertEqual(self.request()[0], 503)

    def test_endpoint_validation(self):
        for value in ("http://public.example/v1/traces", "https://user:pass@host/v1/traces", "https://host/v1/traces?secret=x", "https:///v1/traces", "https://host/v1/traces"):
            with self.assertRaises(ValueError):
                telemetry(value)
        with self.assertRaises(ValueError):
            telemetry("http://127.0.0.1/v1/traces", key="short")
        with self.assertRaises(ValueError):
            origin("https://host/path")

    def test_telemetry_privacy_and_parentage(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        provider, exporter = TracerProvider(), InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.app.close()
        self.app = TaskApplication(provider=provider)
        self.create()
        spans = exporter.get_finished_spans()
        server = next(span for span in spans if span.name == "POST /api/tasks")
        children = [span for span in spans if span.parent and span.parent.span_id == server.context.span_id]
        self.assertTrue(children)
        payload = str([(s.name, dict(s.attributes), s.events) for s in spans])
        self.assertNotIn("Private task", payload)
        self.assertNotIn("Private notes", payload)
        exporter.clear()
        self.request(path="/health")
        self.assertEqual(exporter.get_finished_spans(), ())

    def test_actual_otlp_http_delivery(self):
        import threading
        from server import InvestigatorServer
        receiver = InvestigatorServer(("127.0.0.1", 0), start_workers=False)
        worker = threading.Thread(target=receiver.serve_forever, daemon=True)
        worker.start()
        try:
            database = receiver.catalog.create({"name": "tasks-database", "environment": "development"})
            receiver.catalog.create({"name": "tracewise-tasks", "environment": "development", "dependencies": [database["id"]]})
            receiver.telemetry.configure(True, retain=True)
            provider = telemetry(f"http://127.0.0.1:{receiver.server_port}/v1/traces")
            self.app.close()
            self.app = TaskApplication(provider=provider)
            self.create()
            self.assertTrue(provider.force_flush(timeout_millis=5000))
            self.assertGreaterEqual(receiver.telemetry.status()["spans"], 3)
            self.app.store.db.execute("ALTER TABLE task_items RENAME TO unavailable_tasks")
            self.assertEqual(self.request()[0], 503)
            self.assertTrue(provider.force_flush(timeout_millis=5000))
            self.assertGreaterEqual(receiver.telemetry.status()["incidents"], 1)
            with receiver.store.lock:
                payload = str([row["data"] for row in receiver.store.db.execute("SELECT data FROM telemetry_spans")])
            self.assertNotIn("Private notes", payload)
        finally:
            receiver.shutdown()
            worker.join(timeout=5)
            receiver.server_close()


if __name__ == "__main__":
    unittest.main()
