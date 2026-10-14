import argparse
import json
import os
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler

from task_app.app import TaskApplication, telemetry


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Run the usable personal task application, with optional real-request OTLP export.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8801")))
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--db", default=os.environ.get("TASKBOARD_DB", "storage/taskboard.sqlite3"))
    parser.add_argument("--accounts", default=os.environ.get("TASKBOARD_ACCOUNTS_FILE"))
    parser.add_argument("--public-origin", default=os.environ.get("TASKBOARD_PUBLIC_ORIGIN"))
    parser.add_argument("--telemetry-endpoint", default=os.environ.get("TASKBOARD_OTLP_ENDPOINT"))
    parser.add_argument("--environment", default=os.environ.get("TASKBOARD_ENVIRONMENT", "development"))
    parser.add_argument("--waitress", action="store_true", help="Use the hosted WSGI server instead of local-only wsgiref")
    args = parser.parse_args()
    if args.host == "0.0.0.0" and (not args.accounts or not args.public_origin or not args.waitress):
        parser.error("Non-loopback hosting requires accounts, an HTTPS public origin and --waitress.")
    accounts = json.loads(Path(args.accounts).read_text()) if args.accounts else None
    provider = telemetry(args.telemetry_endpoint, os.environ.get("TRACEWISE_OTLP_KEY"), args.environment) if args.telemetry_endpoint else None
    app = TaskApplication(args.db, accounts, args.public_origin, provider)
    try:
        print(f"Tracewise Tasks: {args.public_origin or 'http://127.0.0.1:' + str(args.port)}", flush=True)
        if args.waitress:
            from waitress import serve
            serve(app, host=args.host, port=args.port, threads=4, connection_limit=32, channel_timeout=15, max_request_body_size=16384, clear_untrusted_proxy_headers=True)
        else:
            with make_server(args.host, args.port, app, handler_class=QuietHandler) as server:
                server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


if __name__ == "__main__":
    main()
