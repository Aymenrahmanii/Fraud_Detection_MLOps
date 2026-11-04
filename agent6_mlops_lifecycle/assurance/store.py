"""Scoped SQLite catalog, content-addressed evidence and durable scheduling checkpoints."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent6_mlops_lifecycle.monitoring.models import MonitorScope

APP_ID = 0x41364153


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class AssuranceStore:
    def __init__(self, path: Path, scope: MonitorScope) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            app = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not ((app == APP_ID and version == 1) or (app == version == 0 and not tables)):
                raise ValueError("Not a supported assurance database")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, "
                "kind TEXT NOT NULL, payload BLOB NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "updated REAL NOT NULL, payload BLOB NOT NULL, digest TEXT NOT NULL)"
            )
            scope_bytes = encode(scope.model_dump(mode="json"))
            existing = connection.execute(
                "SELECT payload FROM documents WHERE kind='scope'"
            ).fetchall()
            if existing and (len(existing) != 1 or existing[0][0] != scope_bytes):
                raise ValueError("Assurance database belongs to another project scope")
            connection.execute(
                "INSERT OR IGNORE INTO documents VALUES (?,?,?)",
                (sha256(scope_bytes).hexdigest(), "scope", scope_bytes),
            )
            connection.execute(f"PRAGMA application_id={APP_ID}")
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection

    def put(self, kind: str, value: object) -> str:
        payload = encode(value)
        identity = sha256(payload).hexdigest()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT kind,payload FROM documents WHERE id=?", (identity,)
            ).fetchone()
            if existing and existing != (kind, payload):
                raise ValueError("Immutable assurance evidence conflicts")
            connection.execute(
                "INSERT OR IGNORE INTO documents VALUES (?,?,?)", (identity, kind, payload)
            )
        return identity

    def get(self, kind: str, identity: str) -> Any:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT kind,payload FROM documents WHERE id=?", (identity,)
            ).fetchone()
        if (
            row is None
            or row[0] != kind
            or len(row[1]) > 16_000_000
            or sha256(row[1]).hexdigest() != identity
        ):
            raise ValueError("Assurance evidence is missing or its digest differs")
        return json.loads(row[1])

    def job(self, identity: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload,digest FROM jobs WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            return None
        if sha256(row[0]).hexdigest() != row[1]:
            raise ValueError("Assurance job checkpoint failed integrity validation")
        result: dict[str, Any] = json.loads(row[0])
        return result

    def save_job(self, identity: str, kind: str, now: datetime, value: dict[str, Any]) -> None:
        payload = encode({**value, "updated_at": now.isoformat()})
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "kind=excluded.kind, updated=excluded.updated, "
                "payload=excluded.payload,digest=excluded.digest",
                (identity, kind, now.timestamp(), payload, sha256(payload).hexdigest()),
            )

    def jobs(self, kind: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            keys = connection.execute(
                "SELECT id FROM jobs WHERE kind=? ORDER BY updated DESC LIMIT 16", (kind,)
            ).fetchall()
        return [value for (key,) in keys if (value := self.job(key)) is not None]
