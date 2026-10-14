import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from task_app.local_stack import connected_stack
from task_app.render_start import arguments


class ConnectedStackTests(unittest.TestCase):
    def test_real_http_crud_reaches_receiver_without_private_content(self):
        with connected_stack(":memory:", ":memory:", 0, 0) as (receiver, app, http):
            url = f"http://127.0.0.1:{http.server_port}"
            with urllib.request.urlopen(url + "/api/tasks", timeout=5) as response:
                self.assertEqual(json.load(response), {"tasks": []})
            request = urllib.request.Request(url + "/api/tasks", data=json.dumps({"title": "Sensitive test title", "description": "Private test description"}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                task = json.load(response)
            request = urllib.request.Request(url + "/api/tasks/" + task["id"], method="PATCH", data=json.dumps({"changes": {"status": "done"}, "version": 1}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.load(response)["status"], "done")
            self.assertTrue(app.provider.force_flush(timeout_millis=5000))
            self.assertGreaterEqual(receiver.telemetry.status()["spans"], 7)
            with receiver.store.lock:
                rows = [json.loads(row["data"]) for row in receiver.store.db.execute("SELECT data FROM telemetry_spans")]
            payload = json.dumps(rows)
            self.assertNotIn("Sensitive test title", payload)
            self.assertNotIn("Private test description", payload)
            self.assertEqual(receiver.telemetry.status()["incidents"], 0)
            self.assertTrue(any(row.get("dependency") == "tasks-database" for row in rows))

    def test_restart_preserves_tasks_without_seed_data(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace, tasks = Path(folder) / "receiver.sqlite3", Path(folder) / "tasks.sqlite3"
            with connected_stack(workspace, tasks, 0, 0) as (_, app, __):
                app.store.create("Local operator", {"title": "Saved by user"})
            with connected_stack(workspace, tasks, 0, 0) as (receiver, app, __):
                self.assertEqual([task["title"] for task in app.store.list("Local operator")], ["Saved by user"])
                self.assertEqual(len(receiver.catalog.list()), 2)

    def test_shared_database_denied(self):
        with self.assertRaisesRegex(ValueError, "separate databases"):
            with connected_stack("same.sqlite3", "same.sqlite3", 0, 0):
                self.fail("Must reject shared databases before opening files")

    def test_render_arguments_require_configuration(self):
        for values in ({}, {"RENDER_EXTERNAL_URL": "https://tasks.example"}, {"TASKBOARD_ACCOUNTS_FILE": "/etc/secrets/tasks.json"}):
            with self.assertRaises(ValueError):
                arguments(values)
        result = arguments({"RENDER_EXTERNAL_URL": "https://tasks.example/", "TASKBOARD_ACCOUNTS_FILE": "/etc/secrets/tasks.json", "PORT": "10000"})
        self.assertIn("--waitress", result)
        self.assertIn("https://tasks.example", result)
        self.assertIn("0.0.0.0", result)


if __name__ == "__main__":
    unittest.main()
