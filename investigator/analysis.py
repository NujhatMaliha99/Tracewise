"""Bounded log parsing, best-effort secret redaction, and deterministic triage."""

import csv
import hashlib
import io
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 10000
SECRET_KEY = re.compile(r"^(?:authorization|proxy.?authorization|password|passwd|pwd|secret|client.?secret|api.?key|access.?token|refresh.?token|token|cookie|set.?cookie)$", re.I)
ASSIGNMENT = re.compile(r"\b(authorization|password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|cookie)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)", re.I)


def redact(value):
    """Best effort; intentionally not a claim of complete PII sanitization."""
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SECRET_KEY.fullmatch(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", value, flags=re.I)
    value = ASSIGNMENT.sub(lambda m: m.group(1) + "=[REDACTED]", value)
    return re.sub(r"(\w+://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)


def timestamp(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def first(row, *keys, default=""):
    return next((row[k] for k in keys if k in row and row[k] not in (None, "")), default)


def normalize_record(raw, number):
    if not isinstance(raw, dict):
        raise ValueError("Record must be an object")
    row = {str(k).lower(): v for k, v in redact(raw).items()}
    message = first(row, "message", "msg", "error", "exception")
    if isinstance(message, (dict, list)):
        message = json.dumps(message)
    if not str(message).strip():
        raise ValueError("Missing message")
    level = str(first(row, "level", "severity", "log_level", default="INFO")).upper()
    level = {"WARNING": "WARN", "CRITICAL": "FATAL", "EXCEPTION": "ERROR"}.get(level, level)
    if level not in {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"}:
        level = "INFO"
    status_raw = first(row, "status", "status_code", "http_status")
    try:
        status = int(status_raw)
        if not 100 <= status <= 599:
            status = None
    except (TypeError, ValueError):
        status = None
    if status and status >= 500 and level not in {"ERROR", "FATAL"}:
        level = "ERROR"
    result = {
        "id": f"L{number:05d}", "line": number,
        "timestamp": timestamp(first(row, "timestamp", "time", "ts", "datetime", default=None)),
        "level": level, "service": str(first(row, "service", "app", "logger", default="unknown"))[:150],
        "message": str(message)[:12000], "request_id": str(first(row, "request_id", "trace_id", "correlation_id"))[:200],
        "method": str(first(row, "method", "http_method"))[:20].upper(),
        "path": str(first(row, "path", "endpoint", "route"))[:1000],
        "status": status,
    }
    for key in ("span_id", "parent_span_id", "dependency"):
        if key in row:
            result[key] = str(row[key])[:150]
    if row.get("span_kind") in ("client", "server"):
        result["span_kind"] = row["span_kind"]
    if "start_time" in row:
        result["start_time"] = timestamp(row["start_time"])
    duration = row.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration <= 3600000 and math.isfinite(duration):
        result["duration_ms"] = duration
    return result


def parse_logs(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Choose a non-empty log file.")
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("File exceeds the 4 MiB limit.")
    content = content.lstrip("\ufeff")
    issues, records = [], []
    stripped = content.strip()
    if stripped.startswith("[") and not re.match(r"\[\d{4}-", stripped):
        try:
            source = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON array near line {exc.lineno}.") from exc
        if not isinstance(source, list):
            raise ValueError("Expected a JSON array.")
        rows = list(enumerate(source, 1))
    elif stripped.splitlines()[0].lower().startswith(("timestamp,", "time,", "level,", "service,")):
        try:
            rows = list(enumerate(csv.DictReader(io.StringIO(content)), 2))
        except csv.Error as exc:
            raise ValueError("Unable to parse CSV.") from exc
    else:
        rows = []
        pattern = re.compile(r"^\[?(?P<timestamp>\d{4}-\d{2}-\d{2}[T ][^\]\s]+)\]?\s+(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\s+(?P<service>[\w.-]+)\s*(?:\||:)\s*(?P<message>.+)$", re.I)
        for number, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                match = pattern.match(line.strip())
                row = match.groupdict() if match else None
            rows.append((number, row))
    if len(rows) > MAX_RECORDS:
        raise ValueError("Too many records. Limit: 10,000 records per investigation.")
    for number, row in rows:
        try:
            records.append(normalize_record(row, number))
        except ValueError as exc:
            issues.append({"line": number, "reason": str(exc)})
    if not records:
        raise ValueError("No usable records. Use JSONL, a JSON array, CSV, or timestamp LEVEL service | message lines.")
    return records, issues


RULES = [
    {
        "key": "connection", "title": "Dependency connection failure",
        "pattern": r"ECONNREFUSED|connection refused|could not connect|connection reset|ECONNRESET",
        "explanation": "The client reports a failed or reset connection to a dependency. These logs alone do not establish whether the service, network, or configuration caused it.",
        "checks": ["Check dependency health and the configured host/port.", "Check network access and connection-reset events.", "Correlate the request ID with dependency-side logs."],
    },
    {
        "key": "timeout", "title": "Dependency request timeout",
        "pattern": r"\btimeout\b|timed out|ETIMEDOUT|deadline exceeded",
        "explanation": "A timeout or exceeded deadline is explicitly recorded. Slow dependencies, congestion, and aggressive timeout settings remain competing hypotheses.",
        "checks": ["Inspect upstream latency around the same timestamps.", "Compare deadlines with the normal latency distribution.", "Check retry volume before increasing timeouts."],
    },
    {
        "key": "pool", "title": "Connection pool exhaustion signal",
        "pattern": r"pool exhausted|pool is full|too many connections|QueuePool limit|connection pool.*(?:limit|exhaust)",
        "explanation": "The logs explicitly mention connection capacity exhaustion. A connection leak, long-running queries, or excess concurrency could explain it; none is proven here.",
        "checks": ["Inspect active connections and pool utilization.", "Check that connections are released after exceptions.", "Inspect long-running queries and concurrency limits."],
    },
    {
        "key": "rate", "title": "Rate limiting observed",
        "pattern": r"rate limit|too many requests|\bHTTP 429\b",
        "explanation": "A rate-limit response or message is present. Quota configuration and burst traffic need independent verification.",
        "checks": ["Inspect retry-after headers and provider quotas.", "Check burst rates by client without exposing identifiers.", "Use bounded backoff and avoid retry amplification."],
    },
    {
        "key": "auth", "title": "Authentication or authorization rejection",
        "pattern": r"unauthorized|forbidden|invalid (?:api key|token)|token expired|expired token",
        "explanation": "The service reports an access rejection. Missing credentials, expiry, and insufficient permissions are possibilities, not established root causes.",
        "checks": ["Check credential presence and expiry without copying secrets.", "Verify the required role or scope.", "Correlate with authentication-service audit events."],
    },
]


def signature(message):
    value = message.lower()
    value = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<id>", value)
    value = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", value)
    value = re.sub(r"(?<=[/=:])\d+\b", "<n>", value)
    value = re.sub(r"\b\d+(?:\.\d+)?(?=(?:ms|sec|seconds|s|mb|kb|bytes)\b)", "<n>", value)
    value = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", value)
    return re.sub(r"\s+", " ", value).strip()


def investigate(content, filename="uploaded logs"):
    records, issues = parse_logs(content)
    errors = [r for r in records if r["level"] in {"ERROR", "FATAL"}]
    grouped = {}
    for record in errors:
        key = (record["service"], signature(record["message"]))
        grouped.setdefault(key, []).append(record)
    groups = []
    for (service, fingerprint), members in grouped.items():
        dates = sorted(r["timestamp"] for r in members if r["timestamp"])
        findings = []
        for rule in RULES:
            evidence = [r["id"] for r in members if re.search(rule["pattern"], r["message"], re.I)]
            if evidence:
                findings.append({k: v for k, v in rule.items() if k != "pattern"} | {"evidence_ids": evidence, "basis": "Explicit log pattern"})
        if not findings:
            findings.append({"key": "unknown", "title": "Insufficient evidence", "explanation": "No supported failure pattern matched these records. Inspect the surrounding request trace and collect dependency metrics before assigning a cause.", "checks": ["Review surrounding events using the request ID.", "Collect a stack trace and dependency-side logs.", "Check recent deploys without assuming causation."], "evidence_ids": [r["id"] for r in members], "basis": "No supported pattern"})
        gid = hashlib.sha256((service + "\0" + fingerprint).encode()).hexdigest()[:12]
        groups.append({"id": gid, "service": service, "signature": fingerprint, "sample": members[0]["message"], "count": len(members), "severity": "FATAL" if any(r["level"] == "FATAL" for r in members) else "ERROR", "first_seen": dates[0] if dates else None, "last_seen": dates[-1] if dates else None, "record_ids": [r["id"] for r in members], "findings": findings})
    groups.sort(key=lambda g: (-g["count"], g["service"], g["id"]))
    dates = sorted(r["timestamp"] for r in records if r["timestamp"])
    buckets = {}
    for r in records:
        if not r["timestamp"]:
            continue
        minute = r["timestamp"][:16] + ":00Z"
        bucket = buckets.setdefault(minute, {"timestamp": minute, "events": 0, "errors": 0, "warnings": 0})
        bucket["events"] += 1
        bucket["errors"] += r["level"] in {"ERROR", "FATAL"}
        bucket["warnings"] += r["level"] == "WARN"
    return {
        "schema_version": 1, "filename": str(redact(filename))[:200],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "Deterministic pattern matching; not an LLM or trained classifier",
        "summary": {"records": len(records), "errors": len(errors), "warnings": sum(r["level"] == "WARN" for r in records), "groups": len(groups), "services": len({r["service"] for r in records}), "skipped": len(issues), "undated": sum(not r["timestamp"] for r in records), "first_seen": dates[0] if dates else None, "last_seen": dates[-1] if dates else None},
        "services": dict(Counter(r["service"] for r in records)),
        "groups": groups, "records": records, "timeline": [buckets[k] for k in sorted(buckets)],
        "parse_issues": issues[:100],
        "limitations": ["Findings are hypotheses, not verified root causes.", "Redaction is best effort; review exports for sensitive information.", "Only ERROR/FATAL or HTTP 5xx records form error groups.", "Missing or invalid timestamps are omitted from the timeline; naive times are interpreted as UTC."],
    }
