"""Evidence-linked trace views and conservative dependency correlation."""

from collections import defaultdict
from datetime import datetime, timezone
from graphlib import TopologicalSorter

from investigator.mesh import SERVICES, EDGES


def seconds(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def is_error(record):
    status = record.get("status")
    return (isinstance(status, int) and status >= 500) or record.get("level") in {"ERROR", "FATAL"}


def summarize(records, window=30, services=None, edges=None):
    services = list(SERVICES if services is None else services)
    edges = list(EDGES if edges is None else edges)
    by_trace = defaultdict(list)
    for index, record in enumerate(records):
        if record.get("request_id"):
            by_trace[record["request_id"]].append(record | {"evidence_id": record.get("evidence_id", f"S{index + 1:05d}")})
    traces, observations = [], []
    parents = {s: set() for s in services}
    for source, target in edges:
        if source in parents and target in parents:
            parents[target].add(source)
    depth = {}
    for service in TopologicalSorter(parents).static_order():
        depth[service] = max((depth[p] + 1 for p in parents[service]), default=0)
    for trace_id, spans in by_trace.items():
        spans.sort(key=lambda s: (s.get("start_time") or "", s["evidence_id"]))
        errors = [s for s in spans if is_error(s)]
        times = [seconds(s.get("timestamp")) for s in errors]
        dated = [t for t in times if t is not None]
        failed_targets = {s["dependency"] for s in errors if s.get("span_kind") == "client" and s.get("dependency") in depth}
        candidates = failed_targets or {s["service"] for s in errors if s.get("service") in depth}
        deepest = max((depth[s] for s in candidates), default=-1)
        anchors = sorted(s for s in candidates if depth[s] == deepest)
        first = min((s for s in errors if seconds(s.get("timestamp")) is not None), key=lambda s: seconds(s["timestamp"]), default=None)
        span_ids = {s.get("span_id") for s in spans}
        traces.append({"id": trace_id, "spans": spans, "errors": len(errors), "services": sorted({s["service"] for s in spans}),
                       "incomplete_parents": sum(bool(s.get("parent_span_id")) and s["parent_span_id"] not in span_ids for s in spans)})
        if errors:
            observations.append({"trace_id": trace_id, "anchors": anchors, "time": min(dated) if dated else None,
                                 "services": {s["service"] for s in errors}, "evidence": [s["evidence_id"] for s in errors],
                                 "first": first})
    observations.sort(key=lambda o: o["time"] if o["time"] is not None else float("inf"))
    episodes = []
    for observation in observations:
        episode = next((e for e in reversed(episodes) if observation["time"] is not None and e["last_time"] is not None
                        and observation["time"] - e["first_time"] <= window and observation["anchors"]
                        and observation["anchors"] == e["suspected_dependencies"]), None)
        if episode is None:
            episode = {"id": f"C{len(episodes) + 1:03d}", "suspected_dependencies": observation["anchors"],
                       "first_time": observation["time"], "last_time": observation["time"], "trace_ids": [],
                       "affected_services": [], "evidence_ids": [], "first_observed_failure": observation["first"]}
            episodes.append(episode)
        episode["last_time"] = observation["time"]
        episode["trace_ids"].append(observation["trace_id"])
        episode["affected_services"] = sorted(set(episode["affected_services"]) | observation["services"])
        episode["evidence_ids"].extend(observation["evidence"])
    nodes = [{"id": service, "depth": depth[service], "spans": sum(r.get("service") == service for r in records),
              "errors": sum(r.get("service") == service and is_error(r) for r in records)} for service in services]
    edges = [{"source": source, "target": target,
              "calls": sum(r.get("service") == source and r.get("dependency") == target for r in records),
              "errors": sum(r.get("service") == source and r.get("dependency") == target and is_error(r) for r in records)} for source, target in edges]
    return {"traces": traces, "episodes": episodes, "nodes": nodes, "edges": edges,
            "window_seconds": window, "limitations": ["Configured topology, not automatic infrastructure discovery.",
            "Deepest observed failed dependency and temporal grouping are hypotheses, not causal proof.",
            "Independent failures can share an anchor; first observation can reflect timeout and collection timing.",
            "Undated errors stay separate; evidence IDs are scoped to this experiment result."]}
