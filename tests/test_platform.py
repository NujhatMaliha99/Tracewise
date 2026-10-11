import json
import tempfile
import unittest
from pathlib import Path

from investigator.fixtures import fixture
from investigator.intelligence import anomaly_analysis, compare, retrieve, runbooks
from investigator.analysis import investigate
from investigator.store import CaseStore, ConflictError


CONTENT = json.dumps({"timestamp": "2026-10-12T09:00:00Z", "level": "ERROR", "service": "api", "message": "Payment timed out"})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()

    def tearDown(self):
        self.store.close()

    def make_case(self, title="Payment timeout"):
        return self.store.create(CONTENT, "logs.jsonl", title)

    def test_create_and_list(self):
        case = self.make_case()
        self.assertEqual(self.store.list()[0]["id"], case["id"])
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["audit"][0]["action"], "created")

    def test_missing_case(self):
        with self.assertRaises(KeyError):
            self.store.get("not-real")

    def test_update_lifecycle(self):
        case = self.make_case()
        updated = self.store.update(case["id"], {"status": "investigating", "owner": "Operator"}, case["version"])
        self.assertEqual(updated["status"], "investigating")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["audit"][0]["detail"]["owner"], "Operator")

    def test_optimistic_conflict(self):
        case = self.make_case()
        self.store.update(case["id"], {"status": "resolved"}, 1)
        with self.assertRaises(ConflictError):
            self.store.update(case["id"], {"status": "open"}, 1)

    def test_archive_is_recoverable(self):
        case = self.make_case()
        archived = self.store.update(case["id"], {"status": "archived"}, 1)
        self.assertFalse(self.store.list())
        self.assertEqual(len(self.store.list(True)), 1)
        self.store.update(case["id"], {"status": "open"}, archived["version"])
        self.assertEqual(len(self.store.list()), 1)

    def test_note_redaction(self):
        case = self.make_case()
        updated = self.store.add_note(case["id"], "Checked token=do-not-retain")
        self.assertNotIn("do-not-retain", json.dumps(updated))
        self.assertEqual(updated["version"], 2)

    def test_blank_note(self):
        case = self.make_case()
        with self.assertRaises(ValueError):
            self.store.add_note(case["id"], " ")

    def test_validation(self):
        for priority in ["invalid", []]:
            with self.assertRaises(ValueError):
                self.store.create(CONTENT, "logs", "Title", priority)
        with self.assertRaises(ValueError):
            self.make_case("")
        case = self.make_case()
        with self.assertRaises(ValueError):
            self.store.update(case["id"], {"bad_field": True}, 1)
        with self.assertRaises(ValueError):
            self.store.update(case["id"], [], 1)

    def test_seed_idempotent(self):
        first = self.store.create(CONTENT, "logs", "Fixture", demo_key="test")
        second = self.store.create(CONTENT, "logs", "Fixture", demo_key="test")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_analytics(self):
        self.make_case()
        result = self.store.analytics()
        self.assertEqual((result["cases"], result["events"], result["errors"]), (1, 1, 1))
        self.assertEqual(result["services"][0]["service"], "api")

    def test_sql_injection_is_literal(self):
        case = self.make_case("'; DROP TABLE cases; --")
        self.assertEqual(self.store.get(case["id"])["title"], "'; DROP TABLE cases; --")
        self.assertEqual(len(self.store.list()), 1)

    def test_disk_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workspace.sqlite3"
            first = CaseStore(path)
            case = first.create(CONTENT, "logs", "Persistent")
            first.close()
            second = CaseStore(path)
            self.assertEqual(second.get(case["id"])["title"], "Persistent")
            second.close()


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()
        self.cases = []
        for index in range(3):
            data = fixture(index)
            self.cases.append(self.store.create(data["content"], data["filename"], data["title"]))

    def tearDown(self):
        self.store.close()

    def test_retrieval_ranking(self):
        result = retrieve("Payment timed out", self.cases)
        if not result["available"]:
            self.skipTest("scikit-learn not installed")
        self.assertEqual(result["matches"][0]["id"], self.cases[0]["id"])
        self.assertGreater(result["matches"][0]["score"], 0)

    def test_retrieval_empty(self):
        self.assertEqual(retrieve("timeout", [])["matches"], [])

    def test_retrieval_exclude(self):
        result = retrieve("Payment", self.cases, self.cases[0]["id"])
        self.assertNotIn(self.cases[0]["id"], [m["id"] for m in result["matches"]])

    def test_retrieval_invalid(self):
        with self.assertRaises(ValueError):
            retrieve("", self.cases)

    def test_short_anomaly_abstention(self):
        result = anomaly_analysis(investigate(CONTENT))
        self.assertFalse(result["available"])
        self.assertIn("12", result["reason"])

    def test_real_model_deterministic(self):
        report = self.cases[0]["report"]
        first = anomaly_analysis(report)
        if not first["available"]:
            self.skipTest("scikit-learn not installed")
        second = anomaly_analysis(report)
        self.assertEqual(first, second)
        self.assertEqual(len(first["windows"]), 24)
        self.assertGreater(first["flagged"], 0)
        self.assertTrue(any(w["flagged"] and w["errors"] == 15 for w in first["windows"]))

    def test_compare(self):
        result = compare(self.cases[0], self.cases[1])
        kinds = {c["kind"] for c in result["changes"]}
        self.assertEqual(kinds, {"new", "absent"})

    def test_fixture_is_explicit(self):
        report = investigate(fixture(0)["content"])
        self.assertEqual(len(report["timeline"]), 24)
        self.assertEqual(report["summary"]["records"], 186)

    def test_runbooks(self):
        self.assertEqual(len(runbooks()), 5)
        self.assertTrue(all(len(r["steps"]) == 3 for r in runbooks()))


if __name__ == "__main__":
    unittest.main()
