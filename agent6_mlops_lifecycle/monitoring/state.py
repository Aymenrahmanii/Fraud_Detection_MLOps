"""Durable monitor reports, baseline approvals, alerts and a single-worker lease."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from agent6_mlops_lifecycle.monitoring.models import (
    DriftBaseline,
    MonitoringReport,
    MonitorScope,
    WorkerStatus,
)
from agent6_mlops_lifecycle.monitoring.source import canonical, utc
from agent6_mlops_lifecycle.monitoring.telemetry_models import OperationalHealth, ServiceProbe

APP_ID = 0x41364D53


class MonitorStateError(RuntimeError):
    pass


class MonitorState:
    def __init__(self, database: Path, scope: MonitorScope) -> None:
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            app = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not ((app == APP_ID and version == 1) or (app == 0 and version == 0 and not tables)):
                raise MonitorStateError("Not a supported monitor state database")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY "
                "CHECK(id=1),owner TEXT NOT NULL,expires TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY "
                "CHECK(id=1),payload BLOB NOT NULL,digest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY "
                "KEY,model_version TEXT NOT NULL,signature TEXT NOT "
                "NULL,assessed TEXT NOT NULL,payload BLOB NOT NULL,digest TEXT "
                "NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS report_time ON reports(model_version,assessed)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS baselines (model_version TEXT "
                "PRIMARY KEY,payload BLOB NOT NULL,digest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY "
                "KEY,report_id TEXT NOT NULL,kind TEXT NOT NULL,created TEXT "
                "NOT NULL,model_version TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operational_health "
                "(model_version TEXT PRIMARY KEY,payload BLOB,digest TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS service_health "
                "(name TEXT PRIMARY KEY,payload BLOB,digest TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS scope_binding (id INTEGER PRIMARY "
                "KEY CHECK(id=1),digest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS outcome_cursors "
                "(source_digest TEXT PRIMARY KEY,sequence INTEGER NOT NULL)"
            )
            scope_digest = sha256(canonical(scope)).hexdigest()
            binding = connection.execute("SELECT digest FROM scope_binding WHERE id=1").fetchone()
            if binding and binding["digest"] != scope_digest:
                raise MonitorStateError("Monitor state belongs to a different project scope")
            connection.execute("INSERT OR IGNORE INTO scope_binding VALUES(1,?)", (scope_digest,))
            connection.execute(f"PRAGMA application_id={APP_ID}")
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.database, timeout=5)) as connection:
                connection.row_factory = sqlite3.Row
                with connection:
                    yield connection
        except sqlite3.Error as error:
            raise MonitorStateError("Monitor state storage is unavailable") from error

    def acquire(self, owner: str, now: datetime, seconds: float) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT owner,expires FROM lease WHERE id=1").fetchone()
            if row and row["owner"] != owner and row["expires"] > utc(now):
                return False
            connection.execute(
                (
                    "INSERT INTO lease VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "owner=excluded.owner,expires=excluded.expires"
                ),
                (owner, utc(now + timedelta(seconds=seconds))),
            )
            return True

    def release(self, owner: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM lease WHERE id=1 AND owner=?", (owner,))

    def save_status(self, status: WorkerStatus) -> None:
        raw = canonical(status)
        with self._connect() as connection:
            connection.execute(
                (
                    "INSERT INTO heartbeat VALUES(1,?,?) ON CONFLICT(id) DO UPDATE "
                    "SET payload=excluded.payload,digest=excluded.digest"
                ),
                (raw, sha256(raw).hexdigest()),
            )

    def status(self) -> WorkerStatus | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload,digest FROM heartbeat WHERE id=1").fetchone()
        return WorkerStatus.model_validate_json(self._verified(row, 65536)) if row else None

    @staticmethod
    def _verified(row: sqlite3.Row, limit: int = 8 * 1024 * 1024) -> bytes:
        raw = bytes(row["payload"])
        if len(raw) > limit or sha256(raw).hexdigest() != row["digest"]:
            raise MonitorStateError("Monitor record integrity check failed")
        return raw

    def latest(self, version: str) -> MonitoringReport | None:
        with self._connect() as connection:
            row = connection.execute(
                (
                    "SELECT payload,digest FROM reports WHERE model_version=? ORDER"
                    " BY assessed DESC,rowid DESC LIMIT 1"
                ),
                (version,),
            ).fetchone()
        return MonitoringReport.model_validate_json(self._verified(row)) if row else None

    def reports(self, limit: int = 100) -> tuple[MonitoringReport, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Report limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload,digest FROM reports ORDER BY assessed DESC,rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(MonitoringReport.model_validate_json(self._verified(row)) for row in rows)

    def save_report(self, report: MonitoringReport, owner: str, now: datetime) -> None:
        raw = canonical(report)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner,expires FROM lease WHERE id=1").fetchone()
            if not lease or lease["owner"] != owner or lease["expires"] <= utc(now):
                raise MonitorStateError("Worker lease was lost before report persistence")
            prior = connection.execute(
                "SELECT payload,digest FROM reports WHERE model_version=? "
                "ORDER BY assessed DESC,rowid DESC LIMIT 1",
                (report.model_version,),
            ).fetchone()
            previous = (
                MonitoringReport.model_validate_json(self._verified(prior)) if prior else None
            )
            prior_kinds = set()
            if previous is not None:
                if previous.fraud_evaluation and previous.fraud_evaluation.status == "FAIL":
                    prior_kinds.add("FRAUD_RISK")
                if previous.evaluation is not None and previous.evaluation.decision == "FAIL":
                    prior_kinds.add("QUALITY_FAILURE")
                if previous.drift.status == "DETECTED":
                    prior_kinds.add("PREDICTION_SHIFT")
                if previous.feature_drift and previous.feature_drift.status == "DETECTED":
                    prior_kinds.add("FEATURE_SHIFT")
            connection.execute(
                "INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.model_version,
                    report.data_signature,
                    utc(report.assessed_at),
                    raw,
                    sha256(raw).hexdigest(),
                ),
            )
            kinds = []
            if report.fraud_evaluation and report.fraud_evaluation.status == "FAIL":
                kinds.append("FRAUD_RISK")
            if report.evaluation is not None and report.evaluation.decision == "FAIL":
                kinds.append("QUALITY_FAILURE")
            if report.drift.status == "DETECTED":
                kinds.append("PREDICTION_SHIFT")
            if report.feature_drift and report.feature_drift.status == "DETECTED":
                kinds.append("FEATURE_SHIFT")
            for kind in kinds:
                if kind in prior_kinds:
                    continue
                identifier = sha256((report.report_id + kind).encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)",
                    (
                        identifier,
                        report.report_id,
                        kind,
                        utc(report.assessed_at),
                        report.model_version,
                    ),
                )
            connection.execute(
                "DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports "
                "ORDER BY assessed DESC LIMIT 1000)"
            )
            connection.execute(
                "DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts "
                "ORDER BY created DESC LIMIT 1000)"
            )

    def alerts(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alerts ORDER BY created DESC LIMIT 50"
            ).fetchall()
        return [dict(row) for row in rows]

    def baseline(self, version: str) -> DriftBaseline | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload,digest FROM baselines WHERE model_version=?", (version,)
            ).fetchone()
        return DriftBaseline.model_validate_json(self._verified(row, 65536)) if row else None

    def approve_baseline(self, baseline: DriftBaseline) -> DriftBaseline:
        raw = canonical(baseline)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,digest FROM baselines WHERE model_version=?",
                (baseline.model_version,),
            ).fetchone()
            if row:
                original = DriftBaseline.model_validate_json(self._verified(row, 65536))
                if original != baseline:
                    raise ValueError("A baseline already exists for this exact model version")
                return original
            connection.execute(
                "INSERT INTO baselines VALUES(?,?,?)",
                (baseline.model_version, raw, sha256(raw).hexdigest()),
            )
        return baseline

    def save_operational(self, health: OperationalHealth, now: datetime) -> None:
        raw = canonical(health)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT payload,digest FROM operational_health WHERE model_version=?",
                (health.model_version,),
            ).fetchone()
            previous = (
                OperationalHealth.model_validate_json(self._verified(prior)) if prior else None
            )
            for kind in set(health.reasons) - set(previous.reasons if previous else ()):
                key = sha256(raw + kind.encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)",
                    (key, key, kind, utc(now), health.model_version),
                )
            connection.execute(
                "INSERT INTO operational_health VALUES(?,?,?) ON CONFLICT(model_version) "
                "DO UPDATE SET payload=excluded.payload,digest=excluded.digest",
                (health.model_version, raw, sha256(raw).hexdigest()),
            )
            connection.execute(
                "DELETE FROM alerts WHERE id NOT IN "
                "(SELECT id FROM alerts ORDER BY created DESC LIMIT 1000)"
            )

    def save_services(self, probes: dict[str, ServiceProbe], now: datetime) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for name, probe in probes.items():
                prior = connection.execute(
                    "SELECT payload,digest FROM service_health WHERE name=?", (name,)
                ).fetchone()
                previous = (
                    ServiceProbe.model_validate_json(self._verified(prior)) if prior else None
                )
                if probe.state == "DOWN" and (previous is None or previous.state != "DOWN"):
                    raw = canonical(probe)
                    key = sha256(name.encode() + raw).hexdigest()
                    connection.execute(
                        "INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)",
                        (key, key, "SERVICE_DOWN", utc(now), name),
                    )
                if probe.state == "UP" and previous is not None and previous.state == "DOWN":
                    key = sha256(name.encode() + canonical(probe) + b"recovered").hexdigest()
                    connection.execute(
                        "INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)",
                        (key, key, "SERVICE_RECOVERED", utc(now), name),
                    )
                raw = canonical(probe)
                connection.execute(
                    "INSERT INTO service_health VALUES(?,?,?) ON CONFLICT(name) "
                    "DO UPDATE SET payload=excluded.payload,digest=excluded.digest",
                    (name, raw, sha256(raw).hexdigest()),
                )

    def outcome_cursor(self, binding: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sequence FROM outcome_cursors WHERE source_digest=?", (binding,)
            ).fetchone()
        return int(row["sequence"]) if row else 0

    def save_outcome_cursor(self, binding: str, sequence: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO outcome_cursors VALUES(?,?) ON CONFLICT(source_digest) "
                "DO UPDATE SET sequence=MAX(sequence,excluded.sequence)",
                (binding, sequence),
            )
