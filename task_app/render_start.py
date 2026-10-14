"""Render entrypoint; refuse public operation without provisioned accounts."""

import os
import sys


def arguments(environ):
    origin = environ.get("RENDER_EXTERNAL_URL")
    accounts = environ.get("TASKBOARD_ACCOUNTS_FILE")
    if not origin or not accounts:
        raise ValueError("Set TASKBOARD_ACCOUNTS_FILE to a Render secret file; Render must supply RENDER_EXTERNAL_URL.")
    return ["task_app", "--waitress", "--host", "0.0.0.0", "--port", environ.get("PORT", "10000"),
            "--accounts", accounts, "--public-origin", origin.rstrip("/")]


if __name__ == "__main__":
    import runpy
    sys.argv = arguments(os.environ)
    runpy.run_module("task_app", run_name="__main__")
