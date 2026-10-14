import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from investigator.backups import snapshot, unpack, restore, schema
from investigator.limits import RateLimits, category
from investigator.store import CaseStore, ConflictError
from server import InvestigatorServer


class LimitTests(unittest.TestCase):
    def test_refill_and_retry_without_sleep(self):
        clock = [0]
        limits = RateLimits({"write": (2, 60)}, clock=lambda: clock[0])
        self.assertEqual(limits.check("client", "write"), 0)
        self.assertEqual(limits.check("client", "write"), 0)
        self.assertEqual(limits.check("client", "write"), 30)
        clock[0] = 30
        self.assertEqual(limits.check("client", "write"), 0)
        self.assertEqual(limits.status()["rejected"]["write"], 1)

    def test_different_categories_and_clients(self):
        limits = RateLimits({"read": (1, 60)}, clock=lambda: 0)
        self.assertEqual(limits.check("a", "read"), 0)
        self.assertGreater(limits.check("a", "read"), 0)
        self.assertEqual(limits.check("a", "write"), 0)
        self.assertEqual(limits.check("b", "read"), 0)

    def test_global_limit_does_not_consume_on_local_denial(self):
        limits = RateLimits({"read": (1, 60), "global": (2, 60)}, clock=lambda: 0)
        limits.check("a", "read")
        self.assertGreater(limits.check("a", "read"), 0)
        self.assertEqual(limits.check("b", "read"), 0)
        self.assertGreater(limits.check("c", "read"), 0)

    def test_bounded_client_state_and_idle_expiry(self):
        clock = [0]
        limits = RateLimits(clock=lambda: clock[0], max_clients=1)
        limits.check("a", "read")
        self.assertGreater(limits.check("b", "read"), 0)
        self.assertEqual(limits.status()["tracked_buckets"], 1)
        clock[0] = 601
        self.assertEqual(limits.check("b", "read"), 0)

    def test_thread_safe_capacity(self):
        limits = RateLimits({"write": (5, 60)}, clock=lambda: 0)
        results = []
        threads = [threading.Thread(target=lambda: results.append(limits.check("same", "write"))) for _ in range(30)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count(0), 5)

    def test_classification_and_invalid_policies(self):
        for path, expected in (("/api/login", "login"), ("/v1/traces", "ingest"), ("/api/backups/export", "backup"), ("/api/analyze", "analysis"), ("/api/cases/x", "write")):
            self.assertEqual(category("POST", path), expected)
        self.assertEqual(category("GET", "/api/reviews/x"), "read")
        for policies in ({"unknown": [1, 1]}, {"read": [True, 60]}, {"read": [0, 60]}, {"read": "bad"}, []):
            with self.assertRaises(ValueError): RateLimits(policies)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.server = InvestigatorServer(("127.0.0.1", 0), start_workers=False)
        self.store = self.server.store
        self.expected = schema(self.store.db)

    def tearDown(self):
        self.server.server_close()

    def test_consistent_snapshot_manifest_and_restore_case(self):
        case = self.store.create('{"service":"api","level":"ERROR","message":"Controlled backup evidence"}', "sample", "Backup case")
        data, manifest = snapshot(self.store)
        decoded, raw = unpack(data)
        self.assertEqual(decoded["sha256"], manifest["sha256"])
        self.assertGreater(len(raw), 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "restored.sqlite3"
            result = restore(data, output, self.expected, True)
            self.assertTrue(result["quarantined"])
            with closing(CaseStore(output)) as restored:
                self.assertEqual(restored.get(case["id"])["title"], "Backup case")

    def test_restore_quarantines_automation_and_preserves_evidence(self):
        monitor = self.server.monitors.create({"name": "Demo", "url": "http://127.0.0.1:8800/health", "enabled": True}, True)
        self.server.telemetry.configure(True, True)
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO deliveries(event_key,payload,state,attempts,due,created_at) VALUES('old:opened','{}','pending',0,0,'now')")
            self.store.db.execute("INSERT INTO experiments VALUES('example','running','now','{}','{}')")
        data, _ = snapshot(self.store)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "restored.sqlite3"
            restore(data, output, self.expected, True)
            with closing(sqlite3.connect(output)) as connection:
                config = json.loads(connection.execute("SELECT config FROM monitors WHERE id=?", (monitor["id"],)).fetchone()[0])
                self.assertFalse(config["enabled"])
                self.assertEqual(connection.execute("SELECT enabled FROM telemetry_settings").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT state,last_error FROM deliveries").fetchone(), ("failed", "RestoredBackupRequiresReview"))
                self.assertEqual(connection.execute("SELECT status FROM experiments").fetchone()[0], "interrupted")
            self.assertTrue(self.server.monitors.get(monitor["id"])["config"]["enabled"])
            self.assertTrue(self.server.telemetry.status()["enabled"])

    def test_no_overwrite_and_trust_required(self):
        data, _ = snapshot(self.store)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.sqlite3"
            with self.assertRaises(ValueError): restore(data, output, self.expected)
            output.write_bytes(b"keep me")
            with self.assertRaises(ConflictError): restore(data, output, self.expected, True)
            self.assertEqual(output.read_bytes(), b"keep me")

    def test_rejects_checksum_mismatch_and_zip_paths(self):
        data, _ = snapshot(self.store)
        manifest, database = unpack(data)
        with tempfile.TemporaryDirectory() as directory:
            for filename, payload in (("workspace.sqlite3", database + b"changed"), ("../workspace.sqlite3", database)):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    archive.writestr("manifest.json", json.dumps(manifest))
                    archive.writestr(filename, payload)
                with self.assertRaises(ValueError): restore(stream.getvalue(), Path(directory) / "new.sqlite3", self.expected, True)

    def test_rejects_triggers_even_if_archive_checksum_matches(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TRIGGER unexpected AFTER INSERT ON notes BEGIN DELETE FROM cases; END")
        data, _ = snapshot(self.store)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new.sqlite3"
            with self.assertRaises(ValueError): restore(data, output, self.expected, True)
            self.assertFalse(output.exists())

    def test_pending_transaction_is_not_backed_up(self):
        with self.store.transaction():
            self.store.db.execute("UPDATE telemetry_settings SET enabled=1")
            with self.assertRaises(ConflictError): snapshot(self.store)

    def test_malformed_archive_and_missing_members(self):
        with self.assertRaises(ValueError): unpack(b"not a zip")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", "{}")
        with self.assertRaises(ValueError): unpack(stream.getvalue())

    def test_legacy_base_workspace_can_be_restored(self):
        with closing(CaseStore()) as store:
            data, _ = snapshot(store)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "legacy.sqlite3"
            self.assertTrue(restore(data, output, self.expected, True)["quarantined"])


if __name__ == "__main__":
    unittest.main()
