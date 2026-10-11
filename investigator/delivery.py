"""Transactional webhook outbox. At-least-once delivery with bounded retries."""

import json
import time

from investigator.store import now


class Delivery:
    def __init__(self, store, policy, url=None, sender=None, clock=time.time):
        self.store, self.policy, self.url, self.clock = store, policy, url, clock
        self.sender = sender or self._send
        if url:
            policy.validate(url)
        with store.lock, store.db:
            store.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL, due REAL NOT NULL,
                created_at TEXT NOT NULL, last_error TEXT)""")

    def enqueue(self, alert_id, transition, name, reason):
        if not self.url:
            return
        payload = {"text": f"Tracewise: {name} / {transition} / {reason}",
                   "event_id": alert_id + ":" + transition, "transition": transition}
        # Caller owns the incident transaction: no independent commit here.
        self.store.db.execute("INSERT OR IGNORE INTO deliveries(event_key,payload,state,attempts,due,created_at) VALUES(?,?,'pending',0,?,?)",
                              (payload["event_id"], json.dumps(payload), self.clock(), now()))
        self.store.db.execute("UPDATE deliveries SET state='failed',last_error='QueueCapacity' WHERE state='pending' AND id NOT IN (SELECT id FROM deliveries WHERE state='pending' ORDER BY id DESC LIMIT 1000)")
        self.store.db.execute("DELETE FROM deliveries WHERE state IN ('sent','failed') AND id NOT IN (SELECT id FROM deliveries WHERE state IN ('sent','failed') ORDER BY id DESC LIMIT 1000)")

    def _send(self, payload):
        connection, path = self.policy.connection(self.url)
        try:
            connection.request("POST", path, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "User-Agent": "Tracewise/0.4"})
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise OSError("Webhook rejected delivery")
        finally:
            connection.close()

    def flush(self):
        if not self.url:
            return
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM deliveries WHERE state='pending' AND due<=? ORDER BY id LIMIT 1", (self.clock(),)).fetchone()
        if row is None:
            return
        error = None
        try:
            self.sender(json.loads(row["payload"]))
        except Exception as exc:
            error = type(exc).__name__  # Never persist a secret webhook URL from exception text.
        attempts = row["attempts"] + 1
        state = "sent" if error is None else "failed" if attempts >= 5 else "pending"
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE deliveries SET state=?,attempts=?,due=?,last_error=? WHERE id=?",
                                  (state, attempts, self.clock() + min(900, 15 * 2 ** (attempts - 1)), error, row["id"]))

    def status(self):
        with self.store.lock:
            counts = {r["state"]: r["count"] for r in self.store.db.execute("SELECT state,COUNT(*) AS count FROM deliveries GROUP BY state")}
            recent = [dict(r) for r in self.store.db.execute("SELECT event_key,state,attempts,created_at,last_error FROM deliveries ORDER BY id DESC LIMIT 20")]
        return {"configured": bool(self.url), "counts": counts, "recent": recent}

    def retry(self, event_key):
        if not self.url:
            raise ValueError("No webhook configured.")
        with self.store.lock, self.store.db:
            if self.store.db.execute("SELECT COUNT(*) FROM deliveries WHERE state='pending'").fetchone()[0] >= 1000:
                raise ValueError("Pending queue is full.")
            cursor = self.store.db.execute("UPDATE deliveries SET state='pending',attempts=0,due=?,last_error=NULL WHERE event_key=? AND state='failed'", (self.clock(), event_key))
            if not cursor.rowcount:
                raise ValueError("Only a failed delivery can be retried.")
        return {"queued": True}
