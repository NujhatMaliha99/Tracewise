"""Bounded in-memory ingestion, separate from explicitly saved case evidence."""

from collections import deque
from datetime import datetime, timezone
from threading import Lock

from investigator.analysis import parse_logs


class LiveBuffer:
    def __init__(self, capacity=1000):
        self.events = deque(maxlen=capacity)
        self.lock = Lock()
        self.sequence = 0

    def ingest(self, content):
        records, issues = parse_logs(content)
        with self.lock:
            for record in records:
                self.sequence += 1
                self.events.append(record | {"id": f"E{self.sequence:07d}", "received_at": datetime.now(timezone.utc).isoformat()})
        return {"accepted": len(records), "skipped": len(issues), "retention": "In-memory rolling buffer, at most 1,000 events; not saved to SQLite"}

    def snapshot(self):
        with self.lock:
            events = list(self.events)
        recent = events[-100:]
        errors = sum(e["level"] in {"ERROR", "FATAL"} for e in recent)
        fraction = errors / len(recent) if recent else 0
        return {"records": events, "count": len(events), "services": len({e["service"] for e in events}), "recent_count": len(recent), "recent_errors": errors, "recent_error_fraction": fraction, "alert": len(recent) >= 10 and fraction >= .2, "alert_rule": "At least 10 events and >=20% ERROR/FATAL among the latest 100 buffered log events", "retention": "Rolling memory only; clearing or restarting removes the buffer"}

    def clear(self):
        with self.lock:
            self.events.clear()
