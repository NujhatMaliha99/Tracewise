import json
import unittest
from pathlib import Path

from investigator.analysis import MAX_BYTES, investigate, parse_logs, redact, signature, timestamp


def log(message="Request timed out", **kwargs):
    return {"timestamp": "2026-10-12T09:00:00Z", "service": "api", "level": "ERROR", "message": message} | kwargs


class AnalysisTests(unittest.TestCase):
    def test_sample_counts(self):
        result = investigate((Path(__file__).parents[1] / "samples/checkout.jsonl").read_text())
        self.assertEqual(result["summary"]["records"], 14)
        self.assertEqual(result["summary"]["errors"], 7)
        self.assertEqual(result["summary"]["groups"], 4)
        self.assertEqual(result["summary"]["warnings"], 2)

    def test_json_array(self):
        rows, issues = parse_logs(json.dumps([log(), log("Connection refused")]))
        self.assertEqual(len(rows), 2)
        self.assertFalse(issues)

    def test_csv(self):
        rows, _ = parse_logs('timestamp,level,service,message,status\n2026-10-12T09:00:00Z,ERROR,api,"oops, database timeout",504\n')
        self.assertEqual(rows[0]["status"], 504)
        self.assertIn(",", rows[0]["message"])

    def test_plain_text(self):
        rows, _ = parse_logs("2026-10-12T09:00:00Z ERROR gateway | Connection refused")
        self.assertEqual(rows[0]["service"], "gateway")

    def test_bracket_plain_text(self):
        rows, _ = parse_logs("[2026-10-12T09:00:00Z] ERROR gateway | Connection refused")
        self.assertEqual(rows[0]["level"], "ERROR")

    def test_invalid_lines_and_source_number(self):
        rows, issues = parse_logs("not valid\n\n" + json.dumps(log()))
        self.assertEqual(rows[0]["line"], 3)
        self.assertEqual(issues[0]["line"], 1)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            parse_logs(" ")

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            parse_logs(None)

    def test_oversize_rejected(self):
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            parse_logs("x" * (MAX_BYTES + 1))

    def test_record_limit(self):
        with self.assertRaisesRegex(ValueError, "10,000"):
            parse_logs(json.dumps([log()] * 10001))

    def test_missing_message(self):
        rows, issues = parse_logs(json.dumps([{}, log()]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(issues), 1)

    def test_invalid_json_array(self):
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            parse_logs("[bad]")

    def test_aliases(self):
        rows, _ = parse_logs(json.dumps({"ts": "2026-10-12T09:00:00Z", "severity": "CRITICAL", "app": "worker", "msg": "Connection refused", "trace_id": "abc"}))
        self.assertEqual(rows[0]["level"], "FATAL")
        self.assertEqual(rows[0]["request_id"], "abc")

    def test_http_5xx_promoted(self):
        result = investigate(json.dumps(log(level="INFO", status_code=500)))
        self.assertEqual(result["summary"]["errors"], 1)

    def test_warnings_do_not_form_errors(self):
        result = investigate(json.dumps(log(level="WARN")))
        self.assertEqual(result["summary"]["groups"], 0)
        self.assertEqual(result["summary"]["warnings"], 1)

    def test_unknown_is_explicit(self):
        result = investigate(json.dumps(log("Unexpected state")))
        self.assertEqual(result["groups"][0]["findings"][0]["title"], "Insufficient evidence")

    def test_all_known_patterns(self):
        for message, key in [("ECONNREFUSED", "connection"), ("deadline exceeded", "timeout"), ("too many connections", "pool"), ("HTTP 429 rate limit", "rate"), ("unauthorized", "auth")]:
            with self.subTest(key=key):
                result = investigate(json.dumps(log(message)))
                self.assertEqual(result["groups"][0]["findings"][0]["key"], key)

    def test_service_boundary(self):
        result = investigate(json.dumps([log(service="a"), log(service="b")]))
        self.assertEqual(len(result["groups"]), 2)

    def test_numeric_grouping(self):
        result = investigate(json.dumps([log("Timeout after 300ms"), log("Timeout after 500ms")]))
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(signature("active=20"), signature("active=30"))

    def test_findings_have_real_evidence(self):
        result = investigate(json.dumps([log(), log("ECONNREFUSED")]))
        ids = {r["id"] for r in result["records"]}
        for group in result["groups"]:
            for finding in group["findings"]:
                self.assertTrue(set(finding["evidence_ids"]).issubset(ids))

    def test_undated_retained(self):
        result = investigate(json.dumps(log(timestamp="yesterday")))
        self.assertEqual(result["summary"]["undated"], 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertFalse(result["timeline"])

    def test_timezone_conversion(self):
        self.assertEqual(timestamp("2026-10-12T15:00:00+06:00"), "2026-10-12T09:00:00.000Z")

    def test_epoch(self):
        self.assertEqual(timestamp(0), "1970-01-01T00:00:00.000Z")

    def test_naive_is_utc(self):
        self.assertEqual(timestamp("2026-10-12T09:00:00"), "2026-10-12T09:00:00.000Z")

    def test_redact_nested_fields(self):
        clean = redact({"api_key": "secret1", "headers": {"Authorization": "secret2"}, "password": "secret3"})
        self.assertNotIn("secret", json.dumps(clean).lower())

    def test_redact_text_credentials(self):
        text = redact("Authorization: Bearer abc.def.ghi password=hunter2 api_key=privatekey postgres://bob:pass@db/name")
        for value in ["abc.def.ghi", "hunter2", "privatekey", "bob:pass"]:
            self.assertNotIn(value, text)

    def test_redaction_before_export(self):
        result = investigate(json.dumps(log("Timeout token=abcsecret")))
        self.assertNotIn("abcsecret", json.dumps(result))

    def test_timeline_counts(self):
        result = investigate(json.dumps([log(), log(level="INFO"), log(level="WARN")]))
        bucket = result["timeline"][0]
        self.assertEqual((bucket["events"], bucket["errors"], bucket["warnings"]), (3, 1, 1))

    def test_deterministic_group_ids(self):
        content = json.dumps([log(), log()])
        self.assertEqual(investigate(content)["groups"], investigate(content)["groups"])


if __name__ == "__main__":
    unittest.main()
