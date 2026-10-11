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

    def test_admin_operations_and_attributed_audit(self):
        cookie = self.login("admin")
        self.assertEqual(self.request("/api/live/clear", {}, cookie)[0], 200)
        status, _, body = self.request("/api/operations", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["audit"][0]["username"], "admin")

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
