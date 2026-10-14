"""Run the four-service traced HTTP example without modifying Tracewise settings."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigator.mesh import ServiceMesh, SERVICES

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-port", type=int, default=8810)
    parser.add_argument("--output", default="storage/mesh-spans.json")
    args = parser.parse_args()
    mesh = ServiceMesh(Path(__file__).resolve().parents[1] / "storage" / "mesh", {name: args.base_port + i for i, name in enumerate(SERVICES)})
    print("Independent traced service mesh: " + json.dumps(mesh.ports), flush=True)
    print(f"GET http://127.0.0.1:{mesh.ports['gateway']}/work; spans are exported on Ctrl+C.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mesh.close()
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(list(mesh.records), indent=2), encoding="utf-8")
