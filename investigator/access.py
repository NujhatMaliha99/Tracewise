"""Offline-provisioned accounts and bounded, expiring server-side sessions."""

import getpass
import hashlib
import hmac
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie, CookieError
from pathlib import Path


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12-256 characters.")
    salt = salt or secrets.token_hex(16)
    return salt + ":" + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class Access:
    def __init__(self, accounts=None, clock=time.time):
        self.accounts = accounts or {}
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions, self.attempts = {}, []
        self.dummy = password_hash(secrets.token_urlsafe(24))
        for name, account in self.accounts.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 80 or account.get("role") not in {"admin", "operator", "viewer"}:
                raise ValueError("Invalid account configuration.")
            salt, digest = account["hash"].split(":")
            if len(bytes.fromhex(salt)) != 16 or len(bytes.fromhex(digest)) != 64:
                raise ValueError("Invalid password hash.")

    @property
    def enabled(self):
        return bool(self.accounts)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            raise ValueError("Invalid credentials.")
        with self.lock:
            self.attempts = [t for t in self.attempts if self.clock() - t < 60]
            if len(self.attempts) >= 10:
                raise ValueError("Too many login attempts. Retry after one minute.")
            self.attempts.append(self.clock())
            account = self.accounts.get(username)
            encoded = account["hash"] if account else self.dummy
            salt, expected = encoded.split(":")
            actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
            if not account or not hmac.compare_digest(actual, expected):
                raise ValueError("Invalid credentials.")
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > self.clock()}
            if len(self.sessions) >= 100:
                raise ValueError("Session capacity reached. Sign out or wait for expiry.")
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {"username": username, "role": account["role"], "expires": self.clock() + 28800}
            return token

    def key(self, cookie):
        try:
            parsed = SimpleCookie(cookie or "")
            token = parsed["tracewise_session"].value
            return hashlib.sha256(token.encode()).hexdigest()
        except (CookieError, KeyError):
            return None

    def identity(self, cookie):
        if not self.enabled:
            return {"username": "Local operator", "role": "admin"}
        with self.lock:
            item = self.sessions.get(self.key(cookie))
            return item if item and item["expires"] > self.clock() else None

    def logout(self, cookie):
        with self.lock:
            self.sessions.pop(self.key(cookie), None)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Provision a Tracewise account offline; passwords never enter shell arguments.")
    parser.add_argument("--file", default="storage/accounts.json")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=["admin", "operator", "viewer"], required=True)
    args = parser.parse_args()
    path = Path(args.file)
    data = json.loads(path.read_text()) if path.exists() else {}
    password = getpass.getpass("Password (at least 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    data[args.username] = {"role": args.role, "hash": password_hash(password)}
    Access(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("Account saved. Restart the server to apply changes; protect this file with OS permissions.")
