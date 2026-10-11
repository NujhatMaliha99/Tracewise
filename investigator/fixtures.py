"""Deterministic, explicitly synthetic multi-service incident scenarios."""

import json
from datetime import datetime, timedelta, timezone

SCENARIOS = [
    ("payments-timeout", "Payment latency escalation", "checkout-api", "Payment dependency timed out after 3000ms", "high"),
    ("inventory-reset", "Inventory connection failures", "inventory-api", "Dependency connection refused at inventory:8080", "high"),
    ("database-pool", "Catalog database saturation", "catalog-api", "Database connection pool exhausted; active=20 limit=20", "critical"),
    ("auth-expiry", "Expired session token rejection", "auth-api", "Authentication token expired; unauthorized request", "medium"),
    ("provider-quota", "External provider quota pressure", "notifications-api", "HTTP 429 rate limit exceeded", "medium"),
    ("schema-unknown", "Webhook schema regression", "webhook-api", "Unexpected event schema version", "low"),
]


def fixture(index):
    key, title, service, message, priority = SCENARIOS[index]
    start = datetime(2026, 10, 12, 9, tzinfo=timezone.utc) + timedelta(hours=index)
    records = []
    for minute in range(24):
        burst = 16 <= minute <= 18
        count = 20 if burst else 6
        for item in range(count):
            error = item < (15 if burst else int(minute % 5 == 0))
            records.append({"timestamp": (start + timedelta(minutes=minute, seconds=item)).isoformat(), "level": "ERROR" if error else "INFO", "service": service, "message": message if error else "Request completed", "request_id": f"fixture-{key}-{minute}-{item}", "status": (429 if index == 4 else 504 if index == 0 else 503) if error else 200, "path": "/api/events", "duration_ms": 3000 if error else 40 + item})
    return {"key": key, "title": title, "priority": priority, "content": "\n".join(json.dumps(r) for r in records), "filename": key + ".jsonl"}
