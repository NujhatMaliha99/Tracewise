import json
import unittest
from datetime import datetime, timedelta, timezone

from investigator.store import CaseStore


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()

    def tearDown(self):
        self.store.close()

    def save(self, records, source="Test fixture"):
        return self.store.create("\n".join(json.dumps(r) for r in records), "activity.jsonl", "Activity fixture", source=source)

    def test_observed_hours_and_undated(self):
        self.save([
            {"timestamp": "2026-10-12T09:10:00Z", "message": "ok", "level": "INFO"},
            {"timestamp": "2026-10-12T11:10:00+02:00", "message": "timeout", "level": "ERROR"},
            {"timestamp": "2026-10-12T12:10:00Z", "message": "ok", "level": "INFO"},
            {"message": "undated", "level": "ERROR"},
        ])
        result = self.store.analytics()
        self.assertEqual(len(result["activity"]), 2)
        self.assertEqual(result["activity"][0]["events"], 2)
        self.assertEqual(result["activity"][0]["errors"], 1)
        self.assertEqual(result["undated"], 1)
        self.assertEqual(result["events"], 4)

    def test_recent_24_observed_hours(self):
        start = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.save([{"timestamp": (start + timedelta(hours=i)).isoformat(), "message": "ok"} for i in range(27)])
        result = self.store.analytics()
        self.assertEqual(result["activity_total_hours"], 27)
        self.assertEqual(len(result["activity"]), 24)
        self.assertIn("T03:00:00", result["activity"][0]["timestamp"])
        self.assertEqual(result["events"], 27)

    def test_archived_excluded_and_synthetic_label(self):
        case = self.save([{"timestamp": "2026-10-12T09:00:00Z", "message": "ok"}], "Synthetic fixture")
        self.assertEqual(self.store.analytics()["synthetic_cases"], 1)
        self.store.update(case["id"], {"status": "archived"}, case["version"])
        result = self.store.analytics()
        self.assertEqual(result["synthetic_cases"], 0)
        self.assertEqual(result["activity"], [])
