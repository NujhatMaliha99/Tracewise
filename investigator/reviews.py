"""Evidence-first incident reviews and versioned follow-up work, without generated causal claims."""

import json
import time
import uuid
from datetime import datetime, timezone

from investigator.catalog import text
from investigator.observability import seconds, is_error
from investigator.store import ConflictError, now


def markdown_text(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "|", "#"):
        value = value.replace(character, "\\" + character)
    return value


class Reviews:
    def __init__(self, store, telemetry, reliability, clock=time.time):
        self.store, self.telemetry, self.reliability, self.clock = store, telemetry, reliability, clock
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS followups(id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES live_incidents(id),
                    title TEXT NOT NULL, owner TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)

    def _due(self, value):
        if value in (None, ""):
            return None
        timestamp = seconds(value) if isinstance(value, str) else None
        if timestamp is None or not 0 <= timestamp <= self.clock() + 366 * 86400:
            raise ValueError("Deadline must be a valid UTC timestamp no more than one year ahead.")
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    def _related(self, incident_id):
        return [r[0] for r in self.store.db.execute("""WITH RECURSIVE lineage(id) AS (
            SELECT id FROM live_incidents WHERE id=? UNION ALL
            SELECT i.id FROM live_incidents i JOIN lineage l ON i.merged_into=l.id)
            SELECT id FROM lineage LIMIT 1000""", (incident_id,))]

    def actions(self, incident_id):
        with self.store.lock:
            self.telemetry.get(incident_id)
            related = set(self._related(incident_id))
            rows = list(self.store.db.execute("SELECT * FROM followups ORDER BY created_at,id"))
            return [dict(r) | {"overdue": bool(r["due_at"] and seconds(r["due_at"]) < self.clock() and r["status"] not in ("done", "cancelled"))} for r in rows if r["incident_id"] in related]

    def create(self, incident_id, data, actor, retain=False):
        if retain is not True or not isinstance(data, dict) or set(data) != {"title", "owner", "due_at"}:
            raise ValueError("Explicit retention and title, owner, due_at are required.")
        with self.store.transaction():
            item = self.telemetry.get(incident_id)
            if item["status"] == "merged":
                raise ValueError("Add actions to the merge target instead.")
            if self.store.db.execute("SELECT COUNT(*) FROM followups").fetchone()[0] >= 2000:
                raise ValueError("Follow-up limit: 2,000.")
            action_id, timestamp = uuid.uuid4().hex[:12], now()
            self.store.db.execute("INSERT INTO followups VALUES(?,?,?,?,?,'open',1,?,?)",
                                  (action_id, incident_id, text(data["title"], 300, True), text(data["owner"], 100), self._due(data["due_at"]), timestamp, timestamp))
            self.telemetry._event(incident_id, "followup_created", {"action_id": action_id, "title": text(data["title"], 300, True)}, actor)
        return self.get(action_id)

    def get(self, action_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM followups WHERE id=?", (text(action_id, 12, True),)).fetchone()
            if row is None:
                raise KeyError("Follow-up not found.")
            return dict(row)

    def update(self, action_id, data, version, actor):
        if not isinstance(data, dict) or not data or set(data) - {"title", "owner", "due_at", "status"}:
            raise ValueError("Edit title, owner, deadline or status only.")
        with self.store.transaction():
            item = self.get(action_id)
            if type(version) is not int or version != item["version"]:
                raise ConflictError("Follow-up changed; refresh before editing.")
            status = data.get("status", item["status"])
            if status not in ("open", "in_progress", "done", "cancelled"):
                raise ValueError("Invalid follow-up status.")
            updated = item | data
            self.store.db.execute("UPDATE followups SET title=?,owner=?,due_at=?,status=?,version=version+1,updated_at=? WHERE id=?",
                                  (text(updated["title"], 300, True), text(updated["owner"], 100), self._due(updated["due_at"]), status, now(), action_id))
            self.telemetry._event(item["incident_id"], "followup_updated", {"action_id": action_id, "status": status, "owner": text(updated["owner"], 100)}, actor)
        return self.get(action_id)

    def report(self, incident_id):
        with self.store.lock:
            item = self.telemetry.get(incident_id)
            actions = self.actions(incident_id)
            changes = self.reliability.nearby_changes(item)
            related = self._related(incident_id)
            notes = []
            for source in related:
                for event in self.telemetry.get(source)["timeline"]:
                    note = event["detail"].get("note") if isinstance(event["detail"], dict) else None
                    if note:
                        notes.append({"incident_id": source, "timestamp": event["timestamp"], "actor": event["actor"], "note": note})
            notes.sort(key=lambda n: n["timestamp"])
            errors = [r for r in item["records"] if is_error(r)]
            return {"incident": item, "actions": actions, "nearby_changes": changes, "operator_notes": notes,
                    "related_incident_ids": related, "generated_at": now(), "review_state": "draft",
                    "summary": {"retained_records": len(item["records"]), "error_records": len(errors), "requests": len(item["trace_ids"]),
                                "services": len(item["services"]), "outstanding_actions": sum(a["status"] not in ("done", "cancelled") for a in actions)},
                    "limitations": ["Draft assembled from retained evidence; operator review required.",
                                    "Observed failure interval is not confirmed user-impact duration or MTTR.",
                                    "Dependency candidates and nearby changes are not verified causes.",
                                    "Timeline is capped at 200 events per related incident; changes capped at 50; evidence retention is bounded."]}

    def markdown(self, incident_id):
        report = self.report(incident_id)
        item, summary = report["incident"], report["summary"]
        md = markdown_text
        lines = [f"# Incident Review: {md(item['id'])}", "", "DRAFT / operator review required", "",
                 f"Generated: {md(report['generated_at'])}", f"Environment: {md(item['environment'])}",
                 f"Status: {md(item['status'])} / Owner: {md(item['owner'] or 'Unassigned')}",
                 f"Observed interval: {md(item['first_seen'])} to {md(item['last_seen'])}", "", "## Evidence Summary", "",
                 f"{summary['requests']} requests / {summary['retained_records']} retained records / {summary['error_records']} error records.",
                 "Services: " + ", ".join(md(s) for s in item["services"]),
                 "Candidate dependencies (unverified): " + (", ".join(md(s) for s in item["anchor"]) or "Insufficient evidence"),
                 "", "## Operator Notes", ""]
        lines += [f"- {md(n['timestamp'])} / {md(n['actor'])}: {md(n['note'])}" for n in report["operator_notes"]] or ["No operator notes recorded."]
        lines += ["", "## Nearby Changes (Temporal Context Only)", ""]
        lines += [f"- {md(c['occurred_at'])} / {md(c['service'])} / {md(c['kind'])}: {md(c['title'])} / revision {md(c['revision'] or 'Not supplied')}" for c in report["nearby_changes"]] or ["No recorded changes in the context window."]
        lines += ["", "## Follow-Up Actions", "", "| Action | Owner | Deadline UTC | Status |", "| --- | --- | --- | --- |"]
        lines += [f"| {md(a['title'])} | {md(a['owner'] or 'Unassigned')} | {md(a['due_at'] or 'Not set')} | {md(a['status'])} |" for a in report["actions"]]
        lines += ["", "## Timeline (Latest Retained Events)", ""]
        lines += [f"- {md(e['timestamp'])} / {md(e['actor'])} / {md(e['kind'])}: {md(json.dumps(e['detail'], ensure_ascii=True))}" for e in reversed(item["timeline"])]
        lines += ["", "## Evidence Index", "", "| Evidence ID | Request | Service | Event UTC | HTTP / Level |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {md(r['evidence_id'])} | {md(r['request_id'])} | {md(r['service'])} | {md(r.get('timestamp') or 'Undated')} | {md(r.get('status'))} / {md(r.get('level'))} |" for r in item["records"]]
        lines += ["", "## Boundaries", ""] + ["- " + md(s) for s in report["limitations"]]
        return "\n".join(lines) + "\n"
