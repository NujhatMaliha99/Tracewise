"""Small WSGI task app with scoped sessions and metadata-only business-request telemetry."""

import json
import logging
import mimetypes
import sqlite3
from contextlib import nullcontext
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from investigator.access import Access
from investigator.limits import RateLimits, category
from investigator.store import ConflictError
from task_app.store import TaskStore

STATIC = Path(__file__).parent / "web"


def origin(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Public origin must be an exact HTTPS origin without path or credentials.")
    return value


def telemetry(endpoint, key=None, environment="development"):
    parsed = urlsplit(endpoint)
    if parsed.path != "/v1/traces" or parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})):
        raise ValueError("Telemetry requires HTTPS or loopback HTTP at /v1/traces, without URL credentials or queries.")
    from investigator.catalog import environment as valid_environment
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    provider = TracerProvider(resource=Resource.create({"service.name": "tracewise-tasks", "deployment.environment.name": valid_environment(environment)}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": "Bearer " + key} if key else {}, timeout=2), max_queue_size=256, max_export_batch_size=64, schedule_delay_millis=1000))
    return provider


class TaskApplication:
    def __init__(self, database=":memory:", accounts=None, public_origin=None, provider=None):
        self.access = Access(accounts)
        self.public_origin = origin(public_origin) if public_origin else None
        if self.public_origin and not self.access.enabled:
            raise ValueError("Public task hosting requires configured accounts.")
        self.provider = provider
        self.tracer = provider.get_tracer("tracewise-tasks") if provider else None
        self.store = TaskStore(database, self.tracer)
        self.limits = RateLimits()

    def cookie(self, environ):
        try:
            values = SimpleCookie(environ.get("HTTP_COOKIE", ""))
            return "tracewise_session=" + values["taskboard_session"].value
        except (KeyError, CookieError):
            return ""

    def response(self, code, data, headers=(), kind="application/json; charset=utf-8"):
        body = json.dumps(data, ensure_ascii=True).encode() if isinstance(data, (dict, list)) else data
        return code, [("Content-Type", kind), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                      ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                      ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")] + list(headers), body

    def body(self, environ):
        if environ.get("HTTP_TRANSFER_ENCODING"):
            raise ValueError("Transfer encoding is not supported.")
        if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            raise ValueError("Use application/json.")
        length = int(environ.get("CONTENT_LENGTH") or "0")
        if not 0 < length <= 16384:
            raise ValueError("JSON body must contain 1-16,384 bytes.")
        data = json.loads(environ["wsgi.input"].read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def route(self, environ):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        host = environ.get("HTTP_HOST", "")
        allowed = {"127.0.0.1:" + environ.get("SERVER_PORT", "8801"), "localhost:" + environ.get("SERVER_PORT", "8801")}
        if self.public_origin:
            allowed.add(urlsplit(self.public_origin).netloc)
        if host not in allowed:
            return self.response(403, {"error": "Host denied."})
        expected_origin = self.public_origin if self.public_origin and host == urlsplit(self.public_origin).netloc else "http://" + host
        if method not in {"GET", "HEAD"} and environ.get("HTTP_ORIGIN") not in (None, expected_origin):
            return self.response(403, {"error": "Same-origin requests required."})
        identity = self.access.identity(self.cookie(environ))
        group = category(method, "/api/login" if path == "/api/login" else path)
        retry = self.limits.check(environ.get("REMOTE_ADDR", "peer") + ":" + (identity["username"] if identity else "anonymous"), group)
        if retry:
            return self.response(429, {"error": "Too many requests. Try again later."}, [("Retry-After", str(retry))])
        assets = {"/": STATIC / "index.html", "/index.html": STATIC / "index.html", "/app.js": STATIC / "app.js", "/styles.css": STATIC / "styles.css",
                  "/lucide.min.js": STATIC.parents[1] / "web" / "vendor" / "lucide.min.js", "/favicon.svg": STATIC.parents[1] / "web" / "favicon.svg"}
        if path in assets and method == "GET":
            return self.response(200, assets[path].read_bytes(), kind=mimetypes.guess_type(str(assets[path]))[0] or "application/octet-stream")
        if path == "/health" and method == "GET":
            self.store.health()
            return self.response(200, {"status": "ok", "service": "tracewise-tasks", "database": "ready"})
        if path == "/api/session" and method == "GET":
            return self.response(200, {"enabled": self.access.enabled, "identity": {k: identity[k] for k in ("username", "role")} if identity else None, "telemetry_configured": self.provider is not None})
        if path == "/api/login" and method == "POST":
            if not self.access.enabled:
                raise ValueError("Local mode has no login account.")
            data = self.body(environ)
            token = self.access.login(data.get("username"), data.get("password"))
            cookie = f"taskboard_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800" + ("; Secure" if self.public_origin else "")
            return self.response(200, {"signed_in": True}, [("Set-Cookie", cookie)])
        if identity is None:
            return self.response(401, {"error": "Sign in required."})
        if path == "/api/logout" and method == "POST":
            self.access.logout(self.cookie(environ))
            cookie = "taskboard_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if self.public_origin else "")
            return self.response(200, {"signed_out": True}, [("Set-Cookie", cookie)])
        if method not in ("GET", "POST", "PATCH"):
            return self.response(405, {"error": "Method not supported."}, [("Allow", "GET, POST, PATCH")])
        if method != "GET" and identity["role"] == "viewer":
            return self.response(403, {"error": "Read-only account."})
        owner = identity["username"]
        if path == "/api/tasks" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            if set(query) - {"archived"} or query.get("archived", ["0"]) not in (["0"], ["1"]):
                raise ValueError("Choose archived=0 or archived=1.")
            return self.response(200, {"tasks": self.store.list(owner, query.get("archived") == ["1"])})
        if path == "/api/tasks" and method == "POST":
            return self.response(201, self.store.create(owner, self.body(environ)))
        parts = path.strip("/").split("/")
        if len(parts) in (3, 4) and parts[:2] == ["api", "tasks"]:
            task_id = parts[2]
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self.response(200, {"history": self.store.history(owner, task_id)})
            if len(parts) == 3 and method == "GET":
                return self.response(200, self.store.get(owner, task_id))
            if len(parts) == 3 and method == "PATCH":
                data = self.body(environ)
                if set(data) != {"changes", "version"}:
                    raise ValueError("Supply changes and version.")
                return self.response(200, self.store.update(owner, task_id, data["changes"], data["version"]))
        return self.response(404, {"error": "Route not found."})

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        business_request = path == "/api/tasks" or path.startswith("/api/tasks/")
        if self.tracer and business_request:
            from opentelemetry import propagate
            from opentelemetry.trace import SpanKind, Status, StatusCode
            carrier = {name[5:].lower().replace("_", "-"): value for name, value in environ.items() if name.startswith("HTTP_") and name in {"HTTP_TRACEPARENT", "HTTP_TRACESTATE"}}
            route = "/api/tasks/{id}/history" if path.endswith("/history") else "/api/tasks/{id}" if path != "/api/tasks" else path
            context = self.tracer.start_as_current_span(environ.get("REQUEST_METHOD", "GET") + " " + route, context=propagate.extract(carrier), kind=SpanKind.SERVER,
                                                        record_exception=False, set_status_on_exception=False, attributes={"http.route": route, "http.request.method": environ.get("REQUEST_METHOD", "GET")})
        else:
            context = nullcontext(None)
        with context as span:
            try:
                result = self.route(environ)
            except ConflictError as exc:
                result = self.response(409, {"error": str(exc)})
            except KeyError:
                result = self.response(404, {"error": "Task not found."})
            except (ValueError, UnicodeDecodeError) as exc:
                result = self.response(400, {"error": str(exc)})
            except sqlite3.Error:
                result = self.response(503, {"error": "Database unavailable."})
            except Exception:
                logging.getLogger("task_app").error("Unhandled task request failure; private request content omitted.")
                result = self.response(500, {"error": "Request could not be completed."})
            code, headers, body = result
            if span:
                span.set_attribute("http.response.status_code", code)
                if code >= 500:
                    span.set_status(Status(StatusCode.ERROR, "Task request failed"))
        start_response(f"{code} {HTTPStatus(code).phrase}", headers)
        return [body]

    def close(self):
        if self.provider:
            self.provider.shutdown()
        self.store.close()
