import http.client
import json
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from investigator.access import Access, password_hash
from investigator.delivery import Delivery
from investigator.monitoring import MonitorEngine, probe, validate
from investigator.outbound import EgressPolicy, PinnedHTTPSConnection
from investigator.store import CaseStore
from investigator.stream import LiveBuffer
from server import InvestigatorServer
from sdk.tracewise import TracewiseClient


PASSWORD = "correct-horse-test-password"
ACCOUNTS = {role: {"role": role, "hash": password_hash(PASSWORD)} for role in ("viewer", "operator", "admin")}


class EgressTests(unittest.TestCase):
    def setUp(self):
        self.policy = EgressPolicy(["health.example.com"])

    def test_remote_opt_in(self):
        config = {"name": "Public health", "url": "https://health.example.com/health"}
        self.assertEqual(validate(config, self.policy)["url"], config["url"])
        with self.assertRaises(ValueError):
            validate(config)

    def test_reject_url_bypasses(self):
        for url in ["http://health.example.com/", "https://health.example.com:8443/", "https://health.example.com.evil.com/", "https://health.example.com@evil.com/", "https://health.example.com/?secret=x", "https://health.example.com/#x", "https://health.example.com\\evil/", "https://127.0.0.1/", "https://health.example.com/\n"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.policy.validate(url)

    def test_reject_wildcard_configuration(self):
        with self.assertRaises(ValueError):
            EgressPolicy(["*.example.com"])

    def test_reject_private_metadata_and_transition_addresses(self):
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.0.1", "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "::ffff:8.8.8.8", "2002:0808:0808::1", "ff02::1"]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[(family, socket.SOCK_STREAM, 6, "", (ip, 443))]), self.assertRaises(ValueError):
                self.policy.resolve("health.example.com")

    def test_mixed_dns_answer_is_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ["8.8.8.8", "127.0.0.1"]]
        with patch("socket.getaddrinfo", return_value=answers), self.assertRaises(ValueError):
            self.policy.resolve("health.example.com")

    def test_each_check_resolves_again(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", side_effect=[public, private]):
            self.assertEqual(self.policy.resolve("health.example.com"), public[0])
            with self.assertRaises(ValueError):
                self.policy.resolve("health.example.com")

    def test_pinned_socket_preserves_tls_hostname(self):
        answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        conn = PinnedHTTPSConnection("health.example.com", answer)
        conn._context = Mock()
        with patch("socket.socket") as factory, patch("socket.getaddrinfo") as resolver:
            conn.connect()
            factory.return_value.connect.assert_called_once_with(("8.8.8.8", 443))
            conn._context.wrap_socket.assert_called_once_with(factory.return_value, server_hostname="health.example.com")
            resolver.assert_not_called()

    def test_policy_block_is_failed_measurement(self):
        with patch.object(self.policy, "connection", side_effect=ValueError("Blocked")):
            result = probe({"name": "Remote", "url": "https://health.example.com/", "expected_status": 200, "latency_limit": 1000}, self.policy)
        self.assertFalse(result["success"])
        self.assertIsNone(result["status"])
        self.assertIn("blocked", result["reason"])

    def test_redirect_is_not_followed(self):
        conn = Mock()
        conn.getresponse.return_value.status = 302
        with patch.object(self.policy, "connection", return_value=(conn, "/")):
            result = probe({"url": "https://health.example.com/", "expected_status": 200, "latency_limit": 1000}, self.policy)
        self.assertFalse(result["success"])
        self.assertEqual(conn.request.call_count, 1)
        conn.close.assert_called_once()


class AccessTests(unittest.TestCase):
    def test_password_salts_differ(self):
        self.assertNotEqual(password_hash(PASSWORD), password_hash(PASSWORD))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            password_hash("short")

    def test_expiry_logout_and_opaque_storage(self):
        clock = [100]
        access = Access(ACCOUNTS, clock=lambda: clock[0])
        token = access.login("viewer", PASSWORD)
        self.assertNotIn(token, access.sessions)
        cookie = "tracewise_session=" + token
        self.assertEqual(access.identity(cookie)["role"], "viewer")
        clock[0] += 28800
        self.assertIsNone(access.identity(cookie))
        token = access.login("admin", PASSWORD)
        access.logout("tracewise_session=" + token)
        self.assertIsNone(access.identity("tracewise_session=" + token))

    def test_rate_limit_and_reset(self):
        clock = [0]
        access = Access(ACCOUNTS, clock=lambda: clock[0])
        for _ in range(10):
            with self.assertRaisesRegex(ValueError, "Invalid credentials"):
                access.login("missing", PASSWORD)
        with self.assertRaisesRegex(ValueError, "Too many"):
            access.login("admin", PASSWORD)
        clock[0] = 61
        self.assertTrue(access.login("admin", PASSWORD))

    def test_fail_closed_public_origin(self):
        with self.assertRaises(ValueError):
            InvestigatorServer(("127.0.0.1", 0), public_origin="https://tracewise.example.com")


class AuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = InvestigatorServer(("127.0.0.1", 0), accounts=ACCOUNTS, ingest_key="x" * 32)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, cookie=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"} | (extra or {})
        if cookie:
            headers["Cookie"] = cookie
        conn.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def login(self, role):
        status, headers, _ = self.request("/api/login", {"username": role, "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";")[0]

    def test_unauthenticated_data_denied_and_login_redirect(self):
        for path in ["/api/cases", "/api/live", "/api/monitors", "/api/operations"]:
            self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request("/")[0], 302)
        self.assertEqual(self.request("/login.html")[0], 200)
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_viewer_can_analyze_but_cannot_mutate(self):
        cookie = self.login("viewer")
        self.assertEqual(self.request("/api/cases", cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/sample", {}, cookie)[0], 200)
        for path in ["/api/live/clear", "/api/monitors", "/api/workspace/seed", "/api/ingest"]:
            self.assertEqual(self.request(path, {}, cookie)[0], 403)
        self.assertEqual(self.request("/api/operations", cookie=cookie)[0], 403)

    def test_operator_permissions(self):
        cookie = self.login("operator")
        self.assertEqual(self.request("/api/live/clear", {}, cookie)[0], 200)
        self.assertEqual(self.request("/api/monitors", {}, cookie)[0], 403)

    def test_experiments_require_admin_but_trace_analysis_allows_viewer(self):
        cookie = self.login("viewer")
        self.assertEqual(self.request("/api/experiments", cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/experiments", {"retain": True}, cookie)[0], 403)
        self.assertEqual(self.request("/api/experiments/cancel", {}, cookie)[0], 403)
        status = self.request("/api/traces/analyze", {"content": json.dumps({"message": "test", "request_id": "a"})}, cookie)[0]
        self.assertEqual(status, 200)

    def test_admin_operations_and_attributed_audit(self):
        cookie = self.login("admin")
        self.assertEqual(self.request("/api/live/clear", {}, cookie)[0], 200)
        status, _, body = self.request("/api/operations", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["audit"][0]["username"], "admin")

    def test_catalog_and_retention_require_admin(self):
        for role in ("viewer", "operator"):
            cookie = self.login(role)
            self.assertEqual(self.request("/api/catalog", cookie=cookie)[0], 200)
            self.assertEqual(self.request("/api/catalog", {"name": "api"}, cookie)[0], 403)
            self.assertEqual(self.request("/api/telemetry/settings", {"enabled": True, "retain": True}, cookie)[0], 403)
        cookie = self.login("admin")
        self.assertEqual(self.request("/api/catalog", {"name": "api"}, cookie)[0], 200)
        self.assertEqual(self.request("/api/telemetry/settings", {"enabled": True, "retain": True}, cookie)[0], 200)

    def test_otlp_scoped_key_and_session_roles(self):
        self.server.telemetry_key = "t" * 32
        self.server.telemetry.configure(True, True)
        self.assertEqual(self.request("/v1/traces", {}, self.login("viewer"))[0], 403)
        self.assertEqual(self.request("/v1/traces", {}, self.login("operator"))[0], 200)
        status, _, body = self.request("/v1/traces", {}, extra={"Authorization": "Bearer " + "x" * 32})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["code"], 16)
        self.assertEqual(self.request("/v1/traces", {}, extra={"Authorization": "Bearer " + "t" * 32})[0], 200)
        self.assertEqual(self.request("/api/catalog", {"name": "forbidden"}, extra={"Authorization": "Bearer " + "t" * 32})[0], 401)
        self.assertEqual(self.request("/api/live-incidents", extra={"Authorization": "Bearer " + "t" * 32})[0], 401)

    def test_incident_operator_permissions_and_conflict(self):
        from test_telemetry import evidence
        self.server.catalog.create({"name": "api"})
        self.server.telemetry.configure(True, True)
        self.server.telemetry.ingest([evidence()])
        item = self.server.telemetry.incidents()[0]
        path = "/api/live-incidents/" + item["id"]
        body = {"changes": {"owner": "Team"}, "version": item["version"]}
        self.assertEqual(self.request(path, body, self.login("viewer"))[0], 403)
        cookie = self.login("operator")
        self.assertEqual(self.request(path, body, cookie)[0], 200)
        self.assertEqual(self.request(path, body, cookie)[0], 409)

    def test_reliability_objective_permissions_and_history(self):
        from test_reliability import config
        service = self.server.catalog.create({"name": "api"})
        for role in ("viewer", "operator"):
            cookie = self.login(role)
            self.assertEqual(self.request("/api/reliability", cookie=cookie)[0], 200)
            self.assertEqual(self.request("/api/objectives", config(service["id"]), cookie)[0], 403)
        cookie = self.login("admin")
        status, _, body = self.request("/api/objectives", config(service["id"]), cookie)
        self.assertEqual(status, 200)
        item = json.loads(body)
        self.assertEqual(self.request("/api/objectives/" + item["id"] + "/history", cookie=cookie)[0], 200)
        status = self.request("/api/objectives/" + item["id"], {"config": config(target=95), "version": 1}, cookie)[0]
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/objectives/" + item["id"], {"config": config(), "version": 1}, cookie)[0], 409)

    def test_change_and_review_permissions_exports_and_consent(self):
        from test_reliability import record, stamp
        import time
        service = self.server.catalog.create({"name": "api"})
        self.server.telemetry.configure(True, True)
        self.server.telemetry.ingest([record(1, 503, at=time.time()-5)])
        item = self.server.telemetry.incidents()[0]
        change = {"service_id": service["id"], "kind": "deployment", "title": "Controlled release", "revision": "v1", "occurred_at": stamp(time.time()), "note": "Test marker"}
        viewer = self.login("viewer"); operator = self.login("operator")
        self.assertEqual(self.request("/api/changes", {"change": change, "retain": True}, viewer)[0], 403)
        self.assertEqual(self.request("/api/changes", {"change": change}, operator)[0], 400)
        self.assertEqual(self.request("/api/changes", {"change": change, "retain": True}, operator)[0], 200)
        action = {"title": "Check timeout", "owner": "Platform", "due_at": None}
        path = "/api/reviews/" + item["id"]
        self.assertEqual(self.request(path, cookie=viewer)[0], 200)
        self.assertEqual(self.request(path + "/actions", {"action": action, "retain": True}, viewer)[0], 403)
        status, _, body = self.request(path + "/actions", {"action": action, "retain": True}, operator)
        self.assertEqual(status, 200)
        action_id = json.loads(body)["id"]
        payload = {"changes": {"status": "done"}, "version": 1}
        self.assertEqual(self.request("/api/followups/" + action_id, payload, viewer)[0], 403)
        self.assertEqual(self.request("/api/followups/" + action_id, payload, operator)[0], 200)
        status, headers, body = self.request(path + "/report", cookie=viewer)
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"DRAFT", body)
        self.assertEqual(self.request(path + "/export", cookie=viewer)[0], 200)

    def test_reliability_endpoints_fail_closed_and_invalid_payloads(self):
        for path in ("/api/reliability", "/api/changes", "/api/reviews/missing"):
            self.assertEqual(self.request(path)[0], 401)
        admin = self.login("admin")
        self.assertEqual(self.request("/api/objectives", [], admin)[0], 400)
        self.assertEqual(self.request("/api/objectives", {"unknown": "field"}, admin)[0], 400)
        self.assertEqual(self.request("/api/changes", {"change": []}, admin)[0], 400)
        self.assertEqual(self.request("/api/reviews/missing", cookie=admin)[0], 404)

    def test_repeated_small_rejections_deliver_http_error_without_reset(self):
        cookie = self.login("viewer")
        for _ in range(30):
            self.assertEqual(self.request("/api/telemetry/settings", {"enabled": True, "retain": True}, cookie)[0], 403)

    def test_backup_and_safety_permissions_consent_and_download(self):
        from investigator.backups import unpack
        for role in ("viewer", "operator"):
            cookie = self.login(role)
            self.assertEqual(self.request("/api/safety", cookie=cookie)[0], 403)
            self.assertEqual(self.request("/api/backups/export", {"include_workspace": True}, cookie)[0], 403)
        admin = self.login("admin")
        self.assertEqual(self.request("/api/safety", cookie=admin)[0], 200)
        self.assertEqual(self.request("/api/backups/export", {}, admin)[0], 400)
        status, headers, data = self.request("/api/backups/export", {"include_workspace": True}, admin)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(unpack(data)[0]["encrypted"], False)

    def test_rate_limit_returns_429_retry_after_and_ignores_forwarded_headers(self):
        from investigator.limits import RateLimits
        self.server.rate_limits = RateLimits({"read": (1, 60)}, clock=lambda: 0)
        self.assertEqual(self.request("/api/health", extra={"X-Forwarded-For": "192.0.2.1"})[0], 200)
        status, headers, body = self.request("/api/health", extra={"X-Forwarded-For": "192.0.2.2"})
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "60")
        self.assertEqual(json.loads(body)["retry_after"], 60)

    def test_otlp_throttle_preserves_standard_error_encoding(self):
        from investigator.limits import RateLimits
        from google.rpc.status_pb2 import Status
        self.server.rate_limits = RateLimits({"ingest": (1, 60)}, clock=lambda: 0)
        self.server.telemetry_key = "t" * 32
        self.server.telemetry.configure(True, True)
        extra = {"Authorization": "Bearer " + "t" * 32}
        self.assertEqual(self.request("/v1/traces", {}, extra=extra)[0], 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/v1/traces", b"", extra | {"Content-Type": "application/x-protobuf"})
        response = connection.getresponse()
        self.assertEqual(response.status, 429)
        self.assertEqual(response.getheader("Content-Type"), "application/x-protobuf")
        self.assertEqual(Status.FromString(response.read()).code, 8)
        connection.close()

    def test_connection_capacity_rejects_and_recovers(self):
        for _ in range(self.server.connection_limit): self.server.connection_slots.acquire()
        try:
            self.assertEqual(self.request("/api/health")[0], 503)
        finally:
            for _ in range(self.server.connection_limit): self.server.connection_slots.release()
        self.assertEqual(self.request("/api/health")[0], 200)
        self.assertEqual(self.server.connection_denials, 1)

    def test_csrf_origin_denied(self):
        cookie = self.login("admin")
        self.assertEqual(self.request("/api/live/clear", {}, cookie, {"Origin": "https://evil.example"})[0], 403)

    def test_logout_revokes_session(self):
        cookie = self.login("admin")
        self.assertEqual(self.request("/api/logout", {}, cookie)[0], 200)
        self.assertEqual(self.request("/api/cases", cookie=cookie)[0], 401)

    def test_ingest_key_is_write_only(self):
        headers = {"Authorization": "Bearer " + "x" * 32}
        body = {"content": json.dumps({"level": "ERROR", "message": "timeout"})}
        self.assertEqual(self.request("/api/ingest", body, extra=headers)[0], 200)
        self.assertEqual(self.request("/api/cases", extra=headers)[0], 401)
        self.assertEqual(self.request("/api/live/clear", {}, extra=headers)[0], 401)

    def test_python_sdk_authenticated_delivery(self):
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        rejected = TracewiseClient("sdk-test", endpoint)
        self.assertFalse(rejected.event("No ingestion credential"))
        client = TracewiseClient("sdk-test", endpoint, ingest_key="x" * 32)
        self.assertTrue(client.event("Authenticated controlled telemetry"))
        self.assertEqual(self.server.stream.snapshot()["count"], 1)

    def test_public_proxy_cookie_secure(self):
        self.server.public_origin = "https://tracewise.example.com"
        status, headers, _ = self.request("/api/login", {"username": "admin", "password": PASSWORD}, extra={"Host": "tracewise.example.com", "Origin": "https://tracewise.example.com"})
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.store = CaseStore()
        self.clock = [0]
        self.sender = Mock()
        self.policy = EgressPolicy(["hooks.example.com"])
        self.delivery = Delivery(self.store, self.policy, "https://hooks.example.com/notify", self.sender, lambda: self.clock[0])

    def tearDown(self):
        self.store.close()

    def enqueue(self):
        with self.store.lock, self.store.db:
            self.delivery.enqueue("alert1", "opened", "Checkout", "Unexpected HTTP 503")

    def test_idempotent_event_key_and_success(self):
        self.enqueue()
        self.enqueue()
        self.delivery.flush()
        self.delivery.flush()
        self.sender.assert_called_once()
        self.assertEqual(self.delivery.status()["counts"], {"sent": 1})

    def test_rollback_prevents_phantom_notification(self):
        with self.assertRaises(RuntimeError):
            with self.store.lock, self.store.db:
                self.delivery.enqueue("a", "opened", "API", "failure")
                raise RuntimeError("rollback")
        self.assertEqual(self.delivery.status()["counts"], {})

    def test_retry_backoff_terminal_failure_and_manual_retry(self):
        self.sender.side_effect = OSError("secret URL must not persist")
        self.enqueue()
        self.delivery.flush()
        self.delivery.flush()
        self.assertEqual(self.sender.call_count, 1)
        for _ in range(4):
            self.clock[0] += 1000
            self.delivery.flush()
        result = self.delivery.status()
        self.assertEqual(result["counts"], {"failed": 1})
        self.assertEqual(result["recent"][0]["last_error"], "OSError")
        self.delivery.retry("alert1:opened")
        self.sender.side_effect = None
        self.delivery.flush()
        self.assertEqual(self.delivery.status()["counts"], {"sent": 1})

    def test_notification_contains_no_logs_or_target_url(self):
        self.enqueue()
        self.delivery.flush()
        payload = self.sender.call_args.args[0]
        self.assertEqual(set(payload), {"text", "event_id", "transition"})
        self.assertNotIn("https://", json.dumps(payload))

    def test_redirect_is_delivery_failure(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        with patch.object(self.policy, "connection", return_value=(connection, "/notify")):
            with self.assertRaises(OSError):
                self.delivery._send({"text": "test"})
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_outbox_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/cases.sqlite3"
            store = CaseStore(path)
            delivery = Delivery(store, self.policy, self.delivery.url, self.sender)
            with store.lock, store.db:
                delivery.enqueue("persist", "opened", "API", "failure")
            store.close()
            store = CaseStore(path)
            delivery = Delivery(store, self.policy, self.delivery.url, self.sender)
            delivery.flush()
            self.assertEqual(delivery.status()["counts"], {"sent": 1})
            store.close()

    def test_alert_open_recovery_and_dedup_queue(self):
        ok = [False]
        result = lambda _: {"timestamp": "2026-10-12T00:00:00Z", "status": 200 if ok[0] else 503, "latency": 2, "success": ok[0], "reason": "Expected status" if ok[0] else "Unexpected HTTP 503"}
        engine = MonitorEngine(self.store, LiveBuffer(), probe_fn=result, delivery=self.delivery)
        item = engine.create({"name": "API", "url": "http://127.0.0.1:8000/", "failure_threshold": 1, "recovery_threshold": 1}, True)
        engine.check(item["id"])
        engine.check(item["id"])
        self.assertEqual(self.delivery.status()["counts"], {"pending": 1})
        ok[0] = True
        engine.check(item["id"])
        self.assertEqual(self.delivery.status()["counts"], {"pending": 2})

    def test_pending_capacity_is_bounded_and_visible(self):
        with self.store.lock, self.store.db:
            for i in range(1002):
                self.delivery.enqueue(str(i), "opened", "API", "failure")
        self.assertEqual(self.delivery.status()["counts"]["pending"], 1000)
        self.assertEqual(self.delivery.status()["counts"]["failed"], 2)


if __name__ == "__main__":
    unittest.main()
