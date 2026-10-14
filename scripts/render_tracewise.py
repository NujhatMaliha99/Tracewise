"""Authenticated Render entrypoint; no accounts, no public listener."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import InvestigatorServer


def main():
    origin = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    account_file = os.environ.get("TRACEWISE_ACCOUNTS_FILE")
    key = os.environ.get("TRACEWISE_OTLP_KEY", "")
    if not origin or not account_file or len(key) < 32:
        raise SystemExit("Render requires origin, TRACEWISE_ACCOUNTS_FILE and a 32+ character TRACEWISE_OTLP_KEY.")
    accounts = json.loads(Path(account_file).read_text(encoding="utf-8"))
    with InvestigatorServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))),
                            os.environ.get("TRACEWISE_DB", "storage/render-workspace.sqlite3"),
                            accounts=accounts, public_origin=origin, telemetry_key=key,
                            remote_hosts=os.environ.get("TRACEWISE_REMOTE_HOSTS", "").split(",")) as server:
        print("Tracewise authenticated Render listener ready; ephemeral storage.", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
