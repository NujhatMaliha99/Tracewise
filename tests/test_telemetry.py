import gzip
import http.client
import json
import threading
import tempfile
import unittest

from investigator.catalog import ServiceCatalog
from investigator.store import CaseStore, ConflictError
from investigator.telemetry import TelemetryStore
from investigator import otlp
from server import InvestigatorServer


def evidence(trace="a", span="1", service="api", env="development", failed=True, second=0):
    return {"request_id": trace, "span_id": span, "service": service, "environment": env,
            "timestamp": f"2026-10-14T00:00:{second:02d}Z", "level": "ERROR" if failed else "INFO",
            "status": 503 if failed else 200, "message": "Observed response", "duration_ms": 5}


def export(service="api"):
    request = otlp.ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(key="service.name").value.string_value = service
    span = resource.scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex("01" * 16)
    span.span_id = bytes.fromhex("02" * 8)
    span.start_time_unix_nano = 1791936000000000000
    span.end_time_unix_nano = span.start_time_unix_nano + 5000000
    span.name = "GET /tasks"
    span.kind = 2
    span.status.code = 2
    return request


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore(":memory:")
        self.catalog = ServiceCatalog(self.store)
        self.api = self.catalog.create({"name": "api"})
        self.db = self.catalog.create({"name": "database"})
        self.catalog.update(self.api["id"], {"dependencies": [self.db["id"]]}, 1)
        self.telemetry = TelemetryStore(self.store, self.catalog)

    def tearDown(self):
        self.store.close()

    def enable(self):
        self.telemetry.configure(True, True)

    def test_default_off_and_explicit_consent(self):
        with self.assertRaises(PermissionError): self.telemetry.ingest([evidence()])
        with self.assertRaises(ValueError): self.telemetry.configure(True)
        self.enable()
        self.telemetry.ingest([evidence()])
        self.telemetry.configure(False)
        self.assertEqual(self.telemetry.status()["spans"], 1)

    def test_catalog_cycle_rolls_back(self):
        with self.assertRaises(ValueError): self.catalog.update(self.db["id"], {"dependencies": [self.api["id"]]}, 1)
        self.assertEqual(self.catalog.get(self.db["id"])["dependencies"], [])
        self.assertEqual(self.catalog.get(self.db["id"])["version"], 1)

    def test_catalog_duplicate_and_stale_version(self):
        with self.assertRaises(ConflictError): self.catalog.create({"name": "api"})
        with self.assertRaises(ConflictError): self.catalog.update(self.api["id"], {"owner": "Team"}, 1)

    def test_environment_edges_and_ingest_isolation(self):
        prod = self.catalog.create({"name": "api", "environment": "production"})
        with self.assertRaises(ValueError): self.catalog.update(prod["id"], {"dependencies": [self.db["id"]]}, 1)
        self.enable()
        self.telemetry.ingest([evidence(), evidence(env="production")])
        self.assertEqual(len(self.telemetry.incidents()), 2)
        items = self.telemetry.incidents()
        with self.assertRaises(ValueError): self.telemetry.merge(items[0]["id"], items[1]["id"], items[0]["version"], items[1]["version"], "operator")

    def test_archive_rejects_new_spans(self):
        self.enable()
        self.catalog.update(self.db["id"], {"active": False}, 1)
        self.assertEqual(self.telemetry.ingest([evidence(service="database")])["rejected"], 1)

    def test_incremental_dedup_healthy_and_unknown(self):
        self.enable()
        self.telemetry.ingest([evidence(failed=False)])
        self.assertEqual(self.telemetry.incidents(), [])
        result = self.telemetry.ingest([evidence(), evidence(service="unknown"), evidence(span="2")])
        self.assertEqual(result, {"accepted": 1, "duplicates": 1, "rejected": 1})
        self.assertEqual(len(self.telemetry.incidents()), 1)
        self.assertEqual(self.telemetry.get(self.telemetry.incidents()[0]["id"])["records"][0]["level"], "INFO")

    def test_manual_split_sticky_late_evidence_merge_and_version(self):
        self.enable()
        self.telemetry.ingest([evidence(), evidence(trace="b", second=2)])
        item = self.telemetry.incidents()[0]
        split = self.telemetry.split(item["id"], ["b"], item["version"], "operator")
        self.telemetry.ingest([evidence(trace="b", span="late", second=4)])
        self.assertEqual(len(self.telemetry.get(split["id"])["records"]), 2)
        with self.assertRaises(ConflictError): self.telemetry.update(item["id"], {"owner": "team"}, item["version"], "operator")
        original, separated = self.telemetry.get(item["id"]), self.telemetry.get(split["id"])
        merged = self.telemetry.merge(original["id"], separated["id"], original["version"], separated["version"], "operator")
        self.assertEqual(len(merged["records"]), 3)
        self.assertEqual(self.telemetry.get(split["id"])["status"], "merged")
        self.assertIn("manual_merge", [e["kind"] for e in merged["timeline"]])

    def test_split_requires_proper_subset(self):
        self.enable(); self.telemetry.ingest([evidence()])
        item = self.telemetry.incidents()[0]
        with self.assertRaises(ValueError): self.telemetry.split(item["id"], ["a"], item["version"], "operator")

    def test_recovery_does_not_auto_resolve(self):
        self.enable(); self.telemetry.ingest([evidence(), evidence(span="healthy", failed=False)])
        self.assertEqual(self.telemetry.incidents()[0]["status"], "open")

    def test_monitor_binding_and_recovery_timeline(self):
        self.enable(); self.catalog.bind_monitor("monitor-1", self.api["id"])
        result = {"timestamp": "2026-10-14T00:00:00Z", "status": 503, "latency": 3, "success": False, "reason": "Unexpected HTTP 503"}
        self.telemetry.on_monitor("monitor-1", {}, result, "alert-1")
        result.update(timestamp="2026-10-14T00:00:02Z", status=200, success=True, reason="Healthy")
        self.telemetry.on_monitor("monitor-1", {}, result, "alert-1", recovered=True)
        item = self.telemetry.get(self.telemetry.incidents()[0]["id"])
        self.assertEqual(item["status"], "open")
        self.assertIn("monitor_recovered", [e["kind"] for e in item["timeline"]])
        self.assertEqual(len(item["records"]), 2)

    def test_retention_and_decisions_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/workspace.sqlite3"
            store = CaseStore(path); catalog = ServiceCatalog(store); telemetry = TelemetryStore(store, catalog)
            catalog.create({"name": "api"}); telemetry.configure(True, True); telemetry.ingest([evidence()])
            item = telemetry.incidents()[0]
            telemetry.update(item["id"], {"owner": "Operations", "status": "investigating"}, item["version"], "tester")
            store.close()
            store = CaseStore(path)
            try:
                telemetry = TelemetryStore(store, ServiceCatalog(store))
                self.assertTrue(telemetry.status()["enabled"])
                self.assertEqual(telemetry.get(item["id"])["owner"], "Operations")
            finally:
                store.close()

    def test_late_child_refines_candidate(self):
        self.enable()
        self.telemetry.ingest([evidence() | {"span_kind": "client"}])
        self.telemetry.ingest([evidence(service="database", span="child", failed=False) | {"span_kind": "server", "parent_span_id": "1"}])
        item = self.telemetry.incidents()[0]
        self.assertEqual(item["anchor"], ["database"])
        self.assertEqual(self.telemetry.evidence("development")["edges"][0]["calls"], 1)


class OTLPTests(unittest.TestCase):
    def test_binary_gzip_and_json(self):
        payload = export().SerializeToString()
        for body, kind, encoding in [(payload, "application/x-protobuf", None), (gzip.compress(payload), "application/x-protobuf", "gzip")]:
            rows, rejected = otlp.spans(otlp.decode(body, kind, encoding))
            self.assertEqual(rejected, 0)
            self.assertEqual(rows[0]["request_id"], "01" * 16)
            self.assertIsNone(rows[0]["status"])
        data = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]}, "scopeSpans": [{"spans": [{"traceId": "01" * 16, "spanId": "02" * 8, "startTimeUnixNano": "1791936000000000000", "endTimeUnixNano": "1791936000005000000", "kind": 2, "status": {"code": 2}, "futureField": 1}]}]}], "future": True}
        rows, rejected = otlp.spans(otlp.decode(json.dumps(data).encode(), "application/json"))
        self.assertEqual(len(rows), 1); self.assertEqual(rejected, 0)

    def test_invalid_and_oversized(self):
        with self.assertRaises(ValueError): otlp.decode(b"{", "application/json")
        with self.assertRaises(ValueError): otlp.decode(b"bad", "application/x-protobuf")
        with self.assertRaises(ValueError): otlp.decode(b"bad", "application/json", "gzip")
        corrupted = bytearray(gzip.compress(b"payload")); corrupted[10] = 7
        with self.assertRaises(ValueError): otlp.decode(bytes(corrupted), "application/x-protobuf", "gzip")
        with self.assertRaises(OverflowError): otlp.decode(gzip.compress(b"x" * (otlp.MAX_BODY + 1)), "application/json", "gzip")
        request = export(); request.resource_spans[0].scope_spans[0].spans[0].trace_id = b"x"
        self.assertEqual(otlp.spans(request)[1], 1)

    def test_sdk_exporter_compatibility_and_http_default_off(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.trace import Status, StatusCode
        server = InvestigatorServer(("127.0.0.1", 0), telemetry_key="k" * 32)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/v1/traces", export().SerializeToString(), {"Content-Type": "application/x-protobuf"})
            response = connection.getresponse(); self.assertEqual(response.status, 403); response.read(); connection.close()
            server.catalog.create({"name": "api"}); server.telemetry.configure(True, True)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/v1/traces", export("unregistered").SerializeToString(), {"Content-Type": "application/x-protobuf"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            partial = otlp.ExportTraceServiceResponse.FromString(response.read())
            self.assertEqual(partial.partial_success.rejected_spans, 1)
            connection.close()
            provider = TracerProvider(resource=Resource.create({"service.name": "api", "deployment.environment.name": "development"}))
            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", headers={"Authorization": "Bearer " + "k" * 32})))
            with provider.get_tracer("compat-test").start_as_current_span("real SDK span") as span:
                span.set_status(Status(StatusCode.ERROR, "Dependency unavailable"))
            provider.shutdown()
            self.assertEqual(server.telemetry.status()["spans"], 1)
            self.assertEqual(len(server.telemetry.incidents()), 1)
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_example_propagates_one_trace_across_real_http(self):
        from examples.otel_demo import run
        server = InvestigatorServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            tasks = server.catalog.create({"name": "otel-tasks"})
            server.catalog.create({"name": "otel-gateway", "dependencies": [tasks["id"]]})
            server.telemetry.configure(True, True)
            run(f"http://127.0.0.1:{server.server_port}/v1/traces", requests=1)
            traces = server.telemetry.evidence("development")["traces"]
            self.assertEqual(len(traces), 1)
            self.assertEqual(len(traces[0]["spans"]), 3)
            self.assertEqual(set(traces[0]["services"]), {"otel-gateway", "otel-tasks"})
            self.assertEqual(traces[0]["incomplete_parents"], 0)
            self.assertEqual(server.telemetry.incidents(), [])
        finally:
            server.shutdown(); thread.join(); server.server_close()


if __name__ == "__main__":
    unittest.main()
