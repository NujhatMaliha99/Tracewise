"""Bounded in-process token buckets; proxy headers are never client identities."""

import math
import threading
import time
from collections import OrderedDict


DEFAULTS = {"read": (240, 60), "write": (120, 60), "analysis": (12, 60),
            "ingest": (120, 60), "login": (10, 60), "backup": (2, 60), "global": (600, 60)}


def category(method, path):
    if method == "GET":
        return "read"
    if path == "/api/login":
        return "login"
    if path == "/api/backups/export":
        return "backup"
    if path in {"/api/ingest", "/v1/traces"}:
        return "ingest"
    if path in {"/api/analyze", "/api/anomalies", "/api/search", "/api/compare", "/api/traces/analyze", "/api/demo", "/api/experiments"}:
        return "analysis"
    return "write"


class RateLimits:
    def __init__(self, policies=None, clock=time.monotonic, max_clients=4096):
        if policies is not None and (not isinstance(policies, dict) or set(policies) - set(DEFAULTS)):
            raise ValueError("Unknown rate-limit categories.")
        self.policies = DEFAULTS | (policies or {})
        for policy in self.policies.values():
            if not isinstance(policy, (tuple, list)) or len(policy) != 2 or any(type(v) is not int for v in policy) or not 1 <= policy[0] <= 100000 or not 1 <= policy[1] <= 3600:
                raise ValueError("Rate policies require [capacity, seconds] with positive bounded integers.")
        if type(max_clients) is not int or not 1 <= max_clients <= 10000:
            raise ValueError("Invalid limiter client capacity.")
        self.clock, self.max_clients = clock, max_clients
        self.ttl = max(600, max(p[1] for p in self.policies.values()) * 2)
        self.clients = OrderedDict()
        self.global_bucket = None
        self.lock = threading.Lock()
        self.allowed, self.rejected = 0, {key: 0 for key in DEFAULTS}

    def _refill(self, bucket, policy, at):
        capacity, period = policy
        if bucket is None:
            return [float(capacity), at]
        return [min(capacity, bucket[0] + max(0, at - bucket[1]) * capacity / period), at]

    def check(self, client, group):
        if group not in self.policies or group == "global" or not isinstance(client, str) or len(client) > 200:
            raise ValueError("Invalid limiter scope.")
        with self.lock:
            at = self.clock()
            while self.clients and at - next(iter(self.clients.values()))[1] >= self.ttl:
                self.clients.popitem(last=False)
            key = (client, group)
            if key not in self.clients and len(self.clients) >= self.max_clients:
                self.rejected[group] += 1
                return max(1, math.ceil(self.ttl - (at - next(iter(self.clients.values()))[1])))
            local = self._refill(self.clients.get(key), self.policies[group], at)
            global_bucket = self._refill(self.global_bucket, self.policies["global"], at)
            self.clients[key], self.global_bucket = local, global_bucket
            self.clients.move_to_end(key)
            waits = [(1 - bucket[0]) * policy[1] / policy[0] for bucket, policy in ((local, self.policies[group]), (global_bucket, self.policies["global"])) if bucket[0] < 1]
            if waits:
                self.rejected["global" if global_bucket[0] < 1 else group] += 1
                return max(1, math.ceil(max(waits)))
            local[0] -= 1
            global_bucket[0] -= 1
            self.allowed += 1
            return 0

    def status(self):
        with self.lock:
            return {"policies": {key: {"capacity": value[0], "period_seconds": value[1]} for key, value in self.policies.items()},
                    "allowed": self.allowed, "rejected": dict(self.rejected), "tracked_buckets": len(self.clients),
                    "bucket_limit": self.max_clients, "scope": "Single process; peer address and authenticated account. Forwarded headers ignored."}
