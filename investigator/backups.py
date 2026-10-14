"""Consistent snapshots and offline, non-overwriting, quarantined recovery."""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
import zlib
from contextlib import closing
from pathlib import Path

from investigator.store import ConflictError, now

MAX_DATABASE = 64 * 1024 * 1024
MAX_ARCHIVE = MAX_DATABASE + 65536
FORMAT = "tracewise-workspace-v1"


def schema(connection):
    return {r["name"]: (r["type"], r["sql"]) for r in connection.execute("SELECT name,type,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}


def snapshot(store):
    with tempfile.TemporaryDirectory(prefix="tracewise-backup-") as directory:
        target = Path(directory) / "workspace.sqlite3"
        with store.lock:
            if store.db.in_transaction:
                raise ConflictError("Finish workspace writes before creating a backup.")
            pages = store.db.execute("PRAGMA page_count").fetchone()[0]
            page_size = store.db.execute("PRAGMA page_size").fetchone()[0]
            if pages * page_size > MAX_DATABASE:
                raise ValueError("Backup exceeds the 64 MiB database limit.")
            with closing(sqlite3.connect(target)) as destination:
                store.db.backup(destination)
        data = target.read_bytes()
        if len(data) > MAX_DATABASE:
            raise ValueError("Backup exceeds the 64 MiB database limit.")
        manifest = {"format": FORMAT, "created_at": now(), "database_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "encrypted": False,
                    "contains": "All retained workspace SQLite evidence and operational state; no external accounts file or environment configuration."}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
            bundle.writestr("workspace.sqlite3", data)
        result = archive.getvalue()
        if len(result) > MAX_ARCHIVE:
            raise ValueError("Backup archive exceeds its size limit.")
        return result, manifest


def unpack(data):
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE:
        raise ValueError("Invalid backup or archive too large.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = archive.infolist()
            if len(files) != 2 or {f.filename for f in files} != {"manifest.json", "workspace.sqlite3"}:
                raise ValueError("Backup must contain exactly manifest.json and workspace.sqlite3.")
            for file in files:
                limit = 16384 if file.filename == "manifest.json" else MAX_DATABASE
                if file.flag_bits & 1 or file.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or file.file_size > limit:
                    raise ValueError("Encrypted, oversized or unsupported archive member.")
                with archive.open(file) as stream:
                    payload = stream.read(limit + 1)
                if len(payload) > limit:
                    raise ValueError("Expanded backup member exceeds its limit.")
                if file.filename == "manifest.json":
                    manifest = json.loads(payload)
                else:
                    database = payload
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("database_bytes") != len(database) or manifest.get("sha256") != hashlib.sha256(database).hexdigest():
            raise ValueError("Backup format, size or checksum mismatch.")
        if not database.startswith(b"SQLite format 3\x00"):
            raise ValueError("Backup is not a SQLite database.")
        return manifest, database
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, OSError, RuntimeError, zlib.error, RecursionError) as exc:
        raise ValueError("Malformed backup archive.") from exc


def validate(connection, expected):
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA trusted_schema=OFF")
    actual = schema(connection)
    if not {"cases", "notes", "audit"} <= actual.keys() or any(name not in expected or value != expected[name] for name, value in actual.items()):
        raise ValueError("Unexpected or incompatible SQLite schema; use a trusted same-version backup.")
    # Structural equality excludes unexpected triggers, views, indexes and virtual tables.
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign-key check failed.")


def restore(data, output, expected, trust=False):
    if trust is not True:
        raise ValueError("Explicit trust confirmation is required. Checksums are not authentication.")
    manifest, database = unpack(data)
    target = Path(output).resolve()
    if target.exists() or target.is_symlink() or Path(str(target) + "-wal").exists() or Path(str(target) + "-shm").exists():
        raise ConflictError("Restore only to a new database path; existing files are never overwritten.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tracewise-restore-", dir=target.parent) as directory:
        temporary = Path(directory) / "validated.sqlite3"
        temporary.write_bytes(database)
        temporary.chmod(0o600)
        with closing(sqlite3.connect(temporary)) as connection:
            connection.row_factory = sqlite3.Row
            validate(connection, expected)
            tables = schema(connection)
            with connection:
                if "monitors" in tables:
                    for row in connection.execute("SELECT id,config FROM monitors").fetchall():
                        config = json.loads(row["config"])
                        if not isinstance(config, dict):
                            raise ValueError("Invalid restored monitor configuration.")
                        config["enabled"] = False
                        connection.execute("UPDATE monitors SET config=? WHERE id=?", (json.dumps(config), row["id"]))
                if "deliveries" in tables:
                    connection.execute("UPDATE deliveries SET state='failed',last_error='RestoredBackupRequiresReview' WHERE state='pending'")
                if "telemetry_settings" in tables:
                    connection.execute("UPDATE telemetry_settings SET enabled=0 WHERE id=1")
                if "experiments" in tables:
                    connection.execute("UPDATE experiments SET status='interrupted' WHERE status IN ('queued','running')")
                if "access_audit" in tables:
                    connection.execute("INSERT INTO access_audit(timestamp,username,role,path) VALUES(?,?,?,?)", (now(), "offline-restore", "admin", "/offline/restore-quarantined"))
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
            validate(connection, expected)
        # Publish a complete validated file exclusively, without replacing a live database.
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise ConflictError("Restore destination was created by another process; nothing was overwritten.") from None
    return {"output": str(target), "source_created_at": manifest.get("created_at"), "quarantined": True,
            "monitors_enabled": False, "telemetry_enabled": False, "pending_delivery_replay": False}


def main():
    parser = argparse.ArgumentParser(description="Validate or restore a trusted Tracewise workspace backup offline.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, help="New SQLite path; never an existing database")
    parser.add_argument("--trust-backup", action="store_true", help="Confirm this archive comes from a trusted source")
    args = parser.parse_args()
    try:
        if args.archive.stat().st_size > MAX_ARCHIVE:
            parser.error("Backup archive exceeds 64 MiB plus packaging allowance.")
        data = args.archive.read_bytes()
    except OSError:
        parser.error("Backup archive could not be read.")
    # Empty reference workspace supplies the current application's exact schema contract.
    from server import InvestigatorServer
    reference = InvestigatorServer(("127.0.0.1", 0), start_workers=False)
    try:
        expected = schema(reference.store.db)
    finally:
        reference.server_close()
    try:
        if args.output:
            result = restore(data, args.output, expected, args.trust_backup)
        else:
            manifest, database = unpack(data)
            with tempfile.TemporaryDirectory(prefix="tracewise-verify-") as directory:
                path = Path(directory) / "workspace.sqlite3"
                path.write_bytes(database)
                with closing(sqlite3.connect(path)) as connection:
                    validate(connection, expected)
            result = {"valid": True, "manifest": manifest, "warning": "Checksum does not establish authenticity. Only restore archives you trust."}
        print(json.dumps(result, indent=2))
    except (ValueError, ConflictError, sqlite3.Error, OSError) as exc:
        parser.exit(1, "Backup operation failed: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
