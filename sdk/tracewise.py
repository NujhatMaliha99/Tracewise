"""Dependency-free synchronous SDK; delivery failure never replaces an app error."""

import json
import logging
import traceback
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit


class TracewiseClient:
    def __init__(self, service, endpoint="http://127.0.0.1:8790", ingest_key=None):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("SDK endpoint must be an explicit-port loopback HTTP origin.")
        self.endpoint = f"http://127.0.0.1:{parsed.port}/api/ingest"
        self.service = str(service)[:100]
        self.ingest_key = ingest_key
        self.last_error = None
        self.dropped = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def event(self, message, level="INFO", **fields):
        try:
            record = fields | {"timestamp": datetime.now(timezone.utc).isoformat(), "service": self.service, "level": level, "message": str(message)}
            content = json.dumps(record)
            if len(content.encode()) > 65536:
                raise ValueError("SDK event exceeds 64 KiB.")
            payload = json.dumps({"content": content}).encode()
            headers = {"Content-Type": "application/json"}
            if self.ingest_key:
                headers["Authorization"] = "Bearer " + self.ingest_key
            request = urllib.request.Request(self.endpoint, data=payload, headers=headers)
            with self.opener.open(request, timeout=1) as response:
                result = json.loads(response.read(4096))
                if result.get("accepted") != 1:
                    raise ValueError("Event was not accepted.")
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.dropped += 1
            return False

    @contextmanager
    def capture(self, **fields):
        try:
            yield
        except Exception:
            self.event(traceback.format_exc(), "ERROR", **fields)
            raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TracewiseHandler(logging.Handler):
    def __init__(self, client, level=logging.WARNING):
        super().__init__(level)
        self.client = client

    def emit(self, record):
        try:
            self.client.event(self.format(record), record.levelname)
        except Exception as exc:
            self.client.last_error = type(exc).__name__
            self.client.dropped += 1
