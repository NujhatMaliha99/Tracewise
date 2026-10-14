"""Connect the usable task app to an isolated local Tracewise workspace."""

import argparse
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from wsgiref.simple_server import make_server

from server import InvestigatorServer
from task_app.__main__ import QuietHandler
from task_app.app import TaskApplication, telemetry


@contextmanager
def connected_stack(workspace, tasks, receiver_port=8796, task_port=8803):
    if ":memory:" not in (str(workspace), str(tasks)) and Path(workspace).resolve() == Path(tasks).resolve():
        raise ValueError("Use separate databases for tasks and Tracewise.")
    with ExitStack() as cleanup:
        receiver = cleanup.enter_context(InvestigatorServer(("127.0.0.1", receiver_port), workspace, start_workers=False))
        database = receiver.catalog.find("tasks-database", "development")
        if database is None:
            database = receiver.catalog.create({"name": "tasks-database", "environment": "development", "description": "Actual SQLite task storage"})
        if receiver.catalog.find("tracewise-tasks", "development") is None:
            receiver.catalog.create({"name": "tracewise-tasks", "environment": "development", "dependencies": [database["id"]]})
        receiver.telemetry.configure(True, retain=True)
        worker = threading.Thread(target=receiver.serve_forever, daemon=True)
        worker.start()
        def stop_receiver():
            receiver.shutdown()
            worker.join(timeout=5)
        cleanup.callback(stop_receiver)
        provider = telemetry(f"http://127.0.0.1:{receiver.server_port}/v1/traces")
        try:
            app = TaskApplication(tasks, provider=provider)
        except Exception:
            provider.shutdown()
            raise
        cleanup.callback(app.close)
        http = cleanup.enter_context(make_server("127.0.0.1", task_port, app, handler_class=QuietHandler))
        task_worker = threading.Thread(target=http.serve_forever, daemon=True)
        task_worker.start()
        def stop_tasks():
            http.shutdown()
            task_worker.join(timeout=5)
        cleanup.callback(stop_tasks)
        yield receiver, app, http


def main():
    parser = argparse.ArgumentParser(description="Run real tasks and Tracewise together on loopback; no sample tasks or injected failures.")
    parser.add_argument("--receiver-port", type=int, default=8796)
    parser.add_argument("--task-port", type=int, default=8803)
    parser.add_argument("--workspace", default="storage/connected-workspace.sqlite3")
    parser.add_argument("--tasks", default="storage/connected-tasks.sqlite3")
    parser.add_argument("--retain-telemetry", action="store_true", help="Explicitly consent to retaining route/status/timing metadata locally")
    args = parser.parse_args()
    if not args.retain_telemetry:
        parser.error("Pass --retain-telemetry to consent to local metadata retention. Task content is excluded.")
    try:
        with connected_stack(args.workspace, args.tasks, args.receiver_port, args.task_port) as (receiver, _, http):
            print(f"Tasks: http://127.0.0.1:{http.server_port}", flush=True)
            print(f"Tracewise: http://127.0.0.1:{receiver.server_port}/?evidence=live#traces", flush=True)
            print("Loopback only; metadata retention enabled. No sample tasks, SMTP or background monitors. Ctrl+C stops both.", flush=True)
            threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
