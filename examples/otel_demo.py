"""Official SDK + W3C context propagation across two real loopback HTTP services."""

import argparse
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def run(endpoint, failed=False, requests=3, key=None):
    providers = []
    def tracer(service):
        provider = TracerProvider(resource=Resource.create({"service.name": service, "deployment.environment.name": "development"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": "Bearer " + key} if key else {})))
        providers.append(provider)
        return provider.get_tracer("tracewise-http-example")
    gateway, tasks = tracer("otel-gateway"), tracer("otel-tasks")
    class Tasks(BaseHTTPRequestHandler):
        def do_GET(self):
            with tasks.start_as_current_span("GET /tasks", context=propagate.extract({key.lower(): value for key, value in self.headers.items()}), kind=trace.SpanKind.SERVER) as span:
                span.set_attribute("http.response.status_code", 200)
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"tasks":[]}')
        def log_message(self, *args):
            pass
    dependency = ThreadingHTTPServer(("127.0.0.1", 0), Tasks)
    address = f"http://127.0.0.1:{dependency.server_port}/tasks"
    worker = threading.Thread(target=dependency.serve_forever, daemon=True)
    worker.start()
    if failed:
        dependency.shutdown(); worker.join(); dependency.server_close()
    try:
        for _ in range(requests):
            with gateway.start_as_current_span("GET /gateway", kind=trace.SpanKind.SERVER) as root:
                with gateway.start_as_current_span("GET tasks", kind=trace.SpanKind.CLIENT) as client:
                    client.set_attribute("peer.service", "otel-tasks")
                    headers = {}; propagate.inject(headers)
                    try:
                        with urllib.request.urlopen(urllib.request.Request(address, headers=headers), timeout=2) as response:
                            response.read(); client.set_attribute("http.response.status_code", response.status)
                        root.set_attribute("http.response.status_code", 200)
                    except (urllib.error.URLError, TimeoutError):
                        client.set_status(trace.Status(trace.StatusCode.ERROR, "Task service unreachable"))
                        root.set_status(trace.Status(trace.StatusCode.ERROR, "Dependency request failed"))
                        root.set_attribute("http.response.status_code", 503)
    finally:
        if not failed:
            dependency.shutdown(); worker.join(); dependency.server_close()
        for provider in providers:
            provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8793/v1/traces")
    parser.add_argument("--failure", action="store_true", help="Stop the actual dependency before gateway requests")
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--prepare-workspace", action="store_true", help="Register example services in a local unauthenticated workspace")
    parser.add_argument("--retain", action="store_true", help="Explicitly enable durable telemetry retention during preparation")
    args = parser.parse_args()
    parsed = urlparse(args.endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/v1/traces" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("This controlled example only supports a loopback /v1/traces endpoint.")
    if not 1 <= args.requests <= 100:
        parser.error("Choose 1-100 requests.")
    if args.prepare_workspace:
        if not args.retain:
            parser.error("Workspace preparation requires explicit --retain consent.")
        origin = f"http://{parsed.netloc}"
        def post(path, body):
            request = urllib.request.Request(origin + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        with urllib.request.urlopen(origin + "/api/catalog", timeout=5) as response:
            services = json.load(response)["services"]
        for name in ("otel-tasks", "otel-gateway"):
            if not any(s["name"] == name and s["environment"] == "development" for s in services):
                services.append(post("/api/catalog", {"name": name, "owner": "Demo", "description": "Controlled OpenTelemetry HTTP example"}))
        gateway = next(s for s in services if s["name"] == "otel-gateway" and s["environment"] == "development")
        tasks = next(s for s in services if s["name"] == "otel-tasks" and s["environment"] == "development")
        post("/api/catalog/" + gateway["id"], {"version": gateway["version"], "changes": {"dependencies": [tasks["id"]]}})
        post("/api/telemetry/settings", {"enabled": True, "retain": True})
    run(args.endpoint, args.failure, args.requests, os.environ.get("TRACEWISE_OTLP_KEY"))
    print("Completed controlled HTTP requests. Check exporter diagnostics and retained evidence in Tracewise.")
