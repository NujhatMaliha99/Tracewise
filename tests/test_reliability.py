import math
import tempfile
import unittest
from datetime import datetime, timezone

from investigator.catalog import ServiceCatalog
from investigator.store import CaseStore, ConflictError
from investigator.telemetry import TelemetryStore
from investigator.reliability import Reliability, request_sample
from investigator.reviews import Reviews


AT = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()

def record(index=0, status=200, at=AT-60, **extra):
    return {"request_id": "request-" + str(index), "span_id": "span-" + str(index), "service": "api", "environment": "development",
            "origin": "otlp", "span_kind": "server", "timestamp": stamp(at), "level": "ERROR" if status and status >= 500 else "INFO",
            "status": status, "duration_ms": 30, "message": "Controlled test request", "evidence_id": "e" + str(index)} | extra

def config(service_id=None, **extra):
    result = {"name": "API availability", "kind": "availability", "target": 99, "window_hours": 24,
              "latency_ms": 1000, "min_requests": 20, "active": True} | extra
    return result | {"service_id": service_id} if service_id else result


class SampleTests(unittest.TestCase):
    def test_status_failures_and_client_checks_are_not_double_counted(self):
        sample = request_sample([record(), record(1, 404), record(2, 503), record(3, None, level="ERROR"),
                                 record(4, 503, span_kind="client"), record(5, 503, origin="monitor")], AT-3600, AT)
        self.assertEqual((sample["good"], sample["bad"], sample["total"]), (2, 2, 4))

    def test_latency_target_includes_fast_failures(self):
        sample = request_sample([record(duration_ms=1000), record(1, duration_ms=1001), record(2, 503, duration_ms=1)], AT-3600, AT, "latency", 1000)
        self.assertEqual((sample["good"], sample["bad"], sample["p95_ms"]), (1, 2, 1001))

    def test_unknown_http_or_duration_is_not_assumed_success(self):
        sample = request_sample([record(status=None), record(1, duration_ms=None)], AT-3600, AT, "latency")
        self.assertEqual(sample["unknown"], 2)
        self.assertIsNone(sample["good_percent"])

    def test_invalid_durations_are_excluded(self):
        for value in (True, -1, math.nan, math.inf, "20"):
            self.assertEqual(request_sample([record(duration_ms=value)], AT-3600, AT, "latency")["unknown"], 1)

    def test_window_boundary_missing_and_future(self):
        sample = request_sample([record(at=AT-3600), record(1, at=AT), record(2, at=AT+1), record(3, timestamp=None)], AT-3600, AT)
        self.assertEqual((sample["total"], sample["future"], sample["undated"]), (1, 1, 1))

    def test_zero_traffic_does_not_claim_healthy(self):
        sample = request_sample([], AT-3600, AT)
        self.assertIsNone(sample["good_percent"])
        self.assertIsNone(sample["bad_fraction"])


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()
        self.catalog = ServiceCatalog(self.store)
        self.service = self.catalog.create({"name": "api", "owner": "Platform"})
        self.telemetry = TelemetryStore(self.store, self.catalog)
        self.telemetry.configure(True, True)
        self.reliability = Reliability(self.store, self.catalog, self.telemetry, clock=lambda: AT)
        self.reviews = Reviews(self.store, self.telemetry, self.reliability, clock=lambda: AT)

    def tearDown(self):
        self.store.close()

    def objective(self, **extra):
        return self.reliability.save(config(self.service["id"], **extra), "admin")

    def change(self, **extra):
        data = {"service_id": self.service["id"], "kind": "deployment", "title": "Release", "revision": "abc123", "occurred_at": stamp(AT-1800), "note": "Controlled test marker"} | extra
        return self.reliability.change(data, "operator", True)

    def test_error_budget_formula_and_burn_watch(self):
        self.telemetry.ingest([record(i, 503 if i < 10 else 200) for i in range(100)])
        result = self.reliability.evaluate(self.objective())
        self.assertEqual(result["state"], "breached")
        self.assertAlmostEqual(result["allowed_bad_requests"], 1)
        self.assertAlmostEqual(result["budget_consumed_percent"], 1000)
        self.assertEqual(result["budget_remaining_percent"], 0)
        self.assertAlmostEqual(result["burn"]["1h"]["rate"], 10)
        self.assertTrue(result["burn_watch"])

    def test_low_sample_and_empty_data(self):
        item = self.objective()
        empty = self.reliability.evaluate(item)
        self.assertEqual(empty["state"], "insufficient_data")
        self.assertIsNone(empty["budget_remaining_percent"])
        self.assertFalse(empty["burn_watch"])
        self.telemetry.ingest([record()])
        self.assertEqual(self.reliability.evaluate(item)["state"], "insufficient_data")

    def test_incomplete_data_never_marked_within_target(self):
        self.telemetry.ingest([record(i) for i in range(20)] + [record(21, None)])
        self.assertEqual(self.reliability.evaluate(self.objective())["state"], "incomplete_data")

    def test_pause_objective_or_service(self):
        item = self.objective(active=False)
        self.assertEqual(self.reliability.evaluate(item)["state"], "paused")
        item = self.reliability.save(config(), "admin", item["id"], 1)
        self.catalog.update(self.service["id"], {"active": False}, 1)
        self.assertEqual(self.reliability.evaluate(item)["state"], "paused")

    def test_environment_scope(self):
        production = self.catalog.create({"name": "api", "environment": "production"})
        self.reliability.save(config(production["id"]), "admin")
        self.objective()
        self.telemetry.ingest([record(i) for i in range(20)])
        self.assertEqual(self.reliability.dashboard("production")["objectives"][0]["sample"]["total"], 0)
        self.assertEqual(self.reliability.dashboard("development")["objectives"][0]["sample"]["total"], 20)

    def test_objective_version_duplicate_and_audit(self):
        item = self.objective()
        with self.assertRaises(ConflictError): self.objective()
        updated = self.reliability.save(config(target=95), "admin", item["id"], 1)
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(ConflictError): self.reliability.save(config(), "admin", item["id"], 1)
        self.assertEqual(len(self.reliability.history(item["id"])), 2)

    def test_invalid_objective_input(self):
        for patch in ({"target": 100}, {"target": math.nan}, {"target": True}, {"target": 10**1000}, {"window_hours": 0}, {"min_requests": False}, {"kind": []}, {"active": 1}):
            with self.subTest(patch=patch), self.assertRaises(ValueError): self.objective(**patch)

    def test_change_comparison_requires_complete_windows_and_samples(self):
        item = self.change()
        self.assertEqual(self.reliability.impact(item["id"])["state"], "insufficient_data")
        rows = [record(i, at=AT-1801-i) for i in range(20)] + [record(20+i, 503 if i < 10 else 200, at=AT-1799+i) for i in range(20)]
        self.telemetry.ingest(rows)
        impact = self.reliability.impact(item["id"])
        self.assertEqual(impact["state"], "comparable")
        self.assertEqual(impact["error_delta_percentage_points"], 50)
        self.assertIn("Temporal association", impact["interpretation"])

    def test_recent_change_is_still_insufficient(self):
        item = self.change(occurred_at=stamp(AT-10))
        self.assertFalse(self.reliability.impact(item["id"])["after_window_complete"])

    def test_change_consent_validation_and_immutability(self):
        with self.assertRaises(ValueError): self.reliability.change({}, "operator", False)
        with self.assertRaises(ValueError): self.change(occurred_at=stamp(AT+301))
        with self.assertRaises(ValueError): self.change(kind="untrusted")
        item = self.change()
        self.assertEqual(self.reliability.changes("development")[0]["id"], item["id"])
        self.assertEqual(self.reliability.changes("production"), [])

    def test_change_multiline_note_and_secret_redaction(self):
        item = self.change(note="First line\nSecond line password=do-not-store")
        self.assertIn("\n", item["note"])
        self.assertNotIn("do-not-store", item["note"])
        with self.assertRaises(ValueError): self.change(note="bad\x00note")

    def test_review_report_preserves_evidence_notes_and_changes(self):
        self.telemetry.ingest([record(1, 503)])
        incident = self.telemetry.incidents()[0]
        self.change(occurred_at=stamp(AT-100))
        self.telemetry.update(incident["id"], {"note": "Check service logs", "owner": "Team"}, incident["version"], "operator")
        result = self.reviews.report(incident["id"])
        self.assertEqual(result["summary"]["error_records"], 1)
        self.assertEqual(result["review_state"], "draft")
        self.assertEqual(len(result["nearby_changes"]), 1)
        self.assertEqual(result["operator_notes"][0]["note"], "Check service logs")
        md = self.reviews.markdown(incident["id"])
        self.assertIn(result["incident"]["records"][0]["evidence_id"], md)
        self.assertIn("not verified causes", md)

    def test_actions_deadlines_versions_and_cancellation(self):
        self.telemetry.ingest([record(1, 503)])
        incident = self.telemetry.incidents()[0]
        data = {"title": "Improve timeout", "owner": "Team", "due_at": stamp(AT-1)}
        with self.assertRaises(ValueError): self.reviews.create(incident["id"], data, "operator")
        action = self.reviews.create(incident["id"], data, "operator", True)
        self.assertTrue(self.reviews.actions(incident["id"])[0]["overdue"])
        updated = self.reviews.update(action["id"], {"status": "done"}, 1, "operator")
        self.assertEqual(updated["version"], 2)
        self.assertFalse(self.reviews.actions(incident["id"])[0]["overdue"])
        with self.assertRaises(ConflictError): self.reviews.update(action["id"], {"status": "open"}, 1, "operator")
        with self.assertRaises(ValueError): self.reviews.update(action["id"], {"status": []}, 2, "operator")

    def test_merged_review_keeps_source_actions_and_notes(self):
        self.telemetry.ingest([record(1, 503), record(2, 503, at=AT-300)])
        left, right = self.telemetry.incidents()
        action = self.reviews.create(right["id"], {"title": "Investigate", "owner": "", "due_at": None}, "operator", True)
        self.telemetry.merge(left["id"], right["id"], left["version"], right["version"], "operator")
        self.assertEqual(self.reviews.report(left["id"])["actions"][0]["id"], action["id"])
        with self.assertRaises(ValueError): self.reviews.create(right["id"], {"title": "No", "owner": "", "due_at": None}, "operator", True)

    def test_markdown_escapes_untrusted_notes(self):
        self.telemetry.ingest([record(1, 503)])
        item = self.telemetry.incidents()[0]
        self.telemetry.update(item["id"], {"note": "<script> [click](https://example.invalid) |"}, item["version"], "operator")
        md = self.reviews.markdown(item["id"])
        self.assertNotIn("<script>", md)
        self.assertNotIn("[click]", md)

    def test_restart_preserves_objectives_changes_and_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/test.sqlite3"
            store = CaseStore(path); catalog = ServiceCatalog(store); telemetry = TelemetryStore(store, catalog)
            service = catalog.create({"name": "api"})
            reliability = Reliability(store, catalog, telemetry, clock=lambda: AT)
            objective = reliability.save(config(service["id"]), "admin")
            telemetry.configure(True, True)
            telemetry.ingest([record(1, 503)])
            incident = telemetry.incidents()[0]
            change = reliability.change({"service_id": service["id"], "kind": "configuration", "title": "Test", "revision": "v1", "occurred_at": stamp(AT-100), "note": "Retained context"}, "operator", True)
            reviews = Reviews(store, telemetry, reliability, clock=lambda: AT)
            action = reviews.create(incident["id"], {"title": "Verify", "owner": "Platform", "due_at": None}, "operator", True)
            store.close()
            store = CaseStore(path)
            try:
                catalog = ServiceCatalog(store); telemetry = TelemetryStore(store, catalog)
                reliability = Reliability(store, catalog, telemetry, clock=lambda: AT)
                self.assertEqual(reliability.get(objective["id"])["target"], 99)
                self.assertEqual(reliability.get_change(change["id"])["revision"], "v1")
                reviews = Reviews(store, telemetry, reliability, clock=lambda: AT)
                self.assertEqual(reviews.get(action["id"])["owner"], "Platform")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
