import json
import unittest

from investigator.stream import LiveBuffer


class LiveBufferTests(unittest.TestCase):
    def record(self, level="INFO", message="Request completed"):
        return json.dumps({"service": "api", "level": level, "message": message})

    def test_bounded_retention(self):
        stream = LiveBuffer(capacity=3)
        stream.ingest("\n".join(self.record() for _ in range(5)))
        self.assertEqual(stream.snapshot()["count"], 3)
        self.assertEqual(stream.snapshot()["records"][0]["id"], "E0000003")

    def test_redaction(self):
        stream = LiveBuffer()
        stream.ingest(self.record(message="Authorization: Bearer supersecret"))
        self.assertNotIn("supersecret", json.dumps(stream.snapshot()))

    def test_threshold_alert(self):
        stream = LiveBuffer()
        stream.ingest("\n".join(self.record("ERROR" if i < 2 else "INFO") for i in range(10)))
        self.assertTrue(stream.snapshot()["alert"])
        self.assertEqual(stream.snapshot()["recent_error_fraction"], .2)

    def test_small_sample_abstains(self):
        stream = LiveBuffer()
        stream.ingest(self.record("ERROR"))
        self.assertFalse(stream.snapshot()["alert"])

    def test_clear_does_not_reuse_ids(self):
        stream = LiveBuffer()
        stream.ingest(self.record())
        stream.clear()
        self.assertEqual(stream.snapshot()["count"], 0)
        stream.ingest(self.record())
        self.assertEqual(stream.snapshot()["records"][0]["id"], "E0000002")

    def test_recent_window(self):
        stream = LiveBuffer()
        stream.ingest("\n".join([self.record("ERROR")] * 100 + [self.record()] * 100))
        self.assertEqual(stream.snapshot()["recent_errors"], 0)
        self.assertFalse(stream.snapshot()["alert"])
