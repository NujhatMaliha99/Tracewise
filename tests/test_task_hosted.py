"""Hosted WSGI server contract over real loopback HTTP; not a cloud/TLS test."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from investigator.access import password_hash
from task_app.app import TaskApplication


class HostedTaskTests(unittest.TestCase):
    def test_waitress_login_health_and_authorization(self):
        from waitress import create_server
        app = TaskApplication(accounts={"operator": {"role": "operator", "hash": password_hash("isolated-test-password")}}, public_origin="https://tasks.example")
        server = create_server(app, host="127.0.0.1", port=0, threads=2, connection_limit=32,
                               channel_timeout=15, max_request_body_size=16384,
                               clear_untrusted_proxy_headers=True, asyncore_loop_timeout=.1)
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        url = "http://127.0.0.1:" + str(server.effective_port)
        def request(path, data=None, cookie=None):
            headers = {"Host": "tasks.example", "Connection": "close"}
            if cookie:
                headers["Cookie"] = cookie
            if data is not None:
                headers.update({"Content-Type": "application/json", "Origin": "https://tasks.example"})
            req = urllib.request.Request(url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
            return urllib.request.urlopen(req, timeout=5)
        try:
            with request("/health") as response:
                self.assertEqual(json.load(response)["database"], "ready")
            with self.assertRaises(urllib.error.HTTPError) as failed:
                request("/api/tasks")
            self.assertEqual(failed.exception.code, 401)
            failed.exception.close()
            with request("/api/login", {"username": "operator", "password": "isolated-test-password"}) as response:
                cookie = response.headers["Set-Cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("Secure", cookie)
            with request("/api/tasks", {"title": "A real hosted-server request"}, cookie.split(";")[0]) as response:
                self.assertEqual(response.status, 201)
            with request("/api/tasks", cookie=cookie.split(";")[0]) as response:
                self.assertEqual(len(json.load(response)["tasks"]), 1)
        finally:
            server.task_dispatcher.shutdown()
            server.close()
            worker.join(timeout=5)
            app.close()
        self.assertFalse(worker.is_alive(), "Hosted test server did not stop")


if __name__ == "__main__":
    unittest.main()
