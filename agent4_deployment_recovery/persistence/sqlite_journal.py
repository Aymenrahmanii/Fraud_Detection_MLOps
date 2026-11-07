"""Transactional SQLite execution journal with persistent workload locks."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent4_deployment_recovery.core.canonical import canonical_json, canonical_sha256
from agent4_deployment_recovery.core.errors import (
    AuditIntegrityError,
    ConcurrentUpdateError,
    IdempotencyConflictError,
    JournalConflictError,
    MessageReplayError,
    WorkloadBusyError,
)
from agent4_deployment_recovery.core.integration_models import (
    AuditChainReference,
    ClaimedOutboxDelivery,
    ExecutionEventOutcome,
    ExecutionEventV1,
    OutboxDeliveryState,
    OutboxDeliveryStatus,
    OutboxRecord,
    VerifiedExternalAuthorization,
)
from agent4_deployment_recovery.core.schemas import (
    DeploymentPlan,
    ExecutionRecord,
    ExecutionState,
    RecoveryVerification,
)
from agent4_deployment_recovery.core.state_machine import (
    is_terminal,
    validate_transition,
)

GENESIS_HASH = "0" * 64


class SQLiteExecutionJournal:
    """Durable state, idempotency, locking, and append-only audit events."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path == Path(":memory:"):
            raise ValueError(
                "a persistent file path is required; :memory: is not supported"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise JournalConflictError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    workload_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    terminal INTEGER NOT NULL CHECK (terminal IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_workload
                    ON executions(workload_key) WHERE terminal = 0;

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    UNIQUE(execution_id, sequence)
                );
                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                    BEFORE UPDATE ON audit_events
                    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                    BEFORE DELETE ON audit_events
                    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;

                CREATE TABLE IF NOT EXISTS execution_artifacts (
                    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                    artifact_type TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(execution_id, artifact_type)
                );
                CREATE TRIGGER IF NOT EXISTS execution_artifacts_no_update
                    BEFORE UPDATE ON execution_artifacts
                    BEGIN SELECT RAISE(ABORT, 'execution artifacts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS execution_artifacts_no_delete
                    BEFORE DELETE ON execution_artifacts
                    BEGIN SELECT RAISE(ABORT, 'execution artifacts are append-only'); END;

                CREATE TABLE IF NOT EXISTS trusted_message_replays (
                    message_kind TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                    issuer TEXT NOT NULL,
                    claims_hash TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    PRIMARY KEY(message_kind, message_id)
                );
                CREATE TRIGGER IF NOT EXISTS trusted_message_replays_no_update
                    BEFORE UPDATE ON trusted_message_replays
                    BEGIN SELECT RAISE(ABORT, 'trusted message receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trusted_message_replays_no_delete
                    BEFORE DELETE ON trusted_message_replays
                    BEGIN SELECT RAISE(ABORT, 'trusted message receipts are append-only'); END;

                CREATE TABLE IF NOT EXISTS outbox_events (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                    audit_sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(execution_id, audit_sequence)
                );
                CREATE TRIGGER IF NOT EXISTS outbox_events_no_update
                    BEFORE UPDATE ON outbox_events
                    BEGIN SELECT RAISE(ABORT, 'outbox events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS outbox_events_no_delete
                    BEFORE DELETE ON outbox_events
                    BEGIN SELECT RAISE(ABORT, 'outbox events are append-only'); END;

                CREATE TABLE IF NOT EXISTS outbox_delivery_state (
                    outbox_id INTEGER PRIMARY KEY REFERENCES outbox_events(outbox_id),
                    delivery_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('PENDING', 'CLAIMED', 'DELIVERED', 'FAILED')
                    ),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    delivered_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO outbox_delivery_state
                    (outbox_id, delivery_id, state, attempt_count, updated_at)
                    SELECT outbox_id, 'DELIVERY-' || event_id, 'PENDING', 0, created_at
                    FROM outbox_events;

                CREATE TABLE IF NOT EXISTS authorization_permit_reservations (
                    permit_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                    operation_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    issuer TEXT NOT NULL,
                    claims_hash TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    UNIQUE(execution_id, operation_id, plan_hash)
                );
                CREATE TRIGGER IF NOT EXISTS authorization_reservations_no_update
                    BEFORE UPDATE ON authorization_permit_reservations
                    BEGIN SELECT RAISE(ABORT, 'authorization reservations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS authorization_reservations_no_delete
                    BEFORE DELETE ON authorization_permit_reservations
                    BEGIN SELECT RAISE(ABORT, 'authorization reservations are append-only'); END;
                """
            )

    def consume_trusted_message(
        self,
        *,
        message_kind: str,
        message_id: str,
        execution_id: str,
        issuer: str,
        claims_hash: str,
        now: datetime,
    ) -> bool:
        """Atomically consume a signed message identifier exactly once."""
        timestamp = self._utc(now, "message consumption timestamp")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM executions WHERE execution_id = ?", (execution_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(execution_id)
            try:
                connection.execute(
                    "INSERT INTO trusted_message_replays "
                    "(message_kind, message_id, execution_id, issuer, claims_hash, consumed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        message_kind,
                        message_id,
                        execution_id,
                        issuer,
                        claims_hash,
                        self._timestamp(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MessageReplayError(
                    f"{message_kind} message ID has already been consumed"
                ) from exc
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve_authorization_permit(
        self,
        *,
        permit_id: str,
        execution_id: str,
        operation_id: str,
        plan_hash: str,
        issuer: str,
        claims_hash: str,
        authorization: VerifiedExternalAuthorization,
        now: datetime,
    ) -> bool:
        """Atomically consume and persist a mutation authorization reservation."""
        timestamp = self._utc(now, "authorization reservation timestamp")
        content = canonical_json(authorization)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json, operation_id, plan_hash FROM executions "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            if row["operation_id"] != operation_id or row["plan_hash"] != plan_hash:
                raise JournalConflictError(
                    "authorization reservation does not match execution operation and plan"
                )
            current = ExecutionRecord.model_validate_json(row["record_json"])
            if timestamp < current.updated_at:
                raise JournalConflictError(
                    "authorization reservation predates the current execution"
                )
            if (
                authorization.permit_id != permit_id
                or authorization.issuer != issuer
                or authorization.claims_hash != claims_hash
            ):
                raise JournalConflictError(
                    "authorization reservation content does not match signed claims"
                )
            try:
                connection.execute(
                    "INSERT INTO trusted_message_replays "
                    "(message_kind, message_id, execution_id, issuer, claims_hash, consumed_at) "
                    "VALUES ('AUTHORIZATION_PERMIT', ?, ?, ?, ?, ?)",
                    (
                        permit_id,
                        execution_id,
                        issuer,
                        claims_hash,
                        self._timestamp(timestamp),
                    ),
                )
                connection.execute(
                    "INSERT INTO authorization_permit_reservations "
                    "(permit_id, execution_id, operation_id, plan_hash, issuer, claims_hash, "
                    "reserved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        permit_id,
                        execution_id,
                        operation_id,
                        plan_hash,
                        issuer,
                        claims_hash,
                        self._timestamp(timestamp),
                    ),
                )
                connection.execute(
                    "INSERT INTO execution_artifacts "
                    "(execution_id, artifact_type, content_text, content_hash, created_at) "
                    "VALUES (?, 'phase4.external_authorization', ?, ?, ?)",
                    (
                        execution_id,
                        content,
                        content_hash,
                        self._timestamp(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MessageReplayError(
                    "authorization permit was already reserved or conflicts with execution"
                ) from exc
            sequence, event_hash = self._append_event(
                connection,
                execution_id=execution_id,
                event_type="ARTIFACT_RECORDED",
                from_state=current.state,
                to_state=current.state,
                occurred_at=timestamp,
                payload={
                    "artifact_hash": content_hash,
                    "artifact_type": "phase4.external_authorization",
                    "record_hash": self._record_hash(current),
                    "version": current.version,
                },
            )
            self._append_outbox_event(
                connection,
                record=current,
                event_type="EVIDENCE_RECORDED",
                sequence=sequence,
                event_hash=event_hash,
                occurred_at=timestamp,
                evidence_refs=(content_hash,),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_artifact(
        self,
        execution_id: str,
        artifact_type: str,
        content_text: str,
        content_hash: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Append one immutable artifact and audit it in the same transaction.

        An exact replay is idempotent. Reusing an artifact type for different
        bytes is a conflict, preventing a dry-run body from being replaced.
        """
        timestamp = self._utc(now or datetime.now(UTC), "artifact timestamp")
        if not artifact_type or len(artifact_type) > 128:
            raise JournalConflictError("artifact_type must be non-empty and bounded")
        expected_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        if content_hash != expected_hash:
            raise JournalConflictError(
                "artifact hash does not match exact content bytes"
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            current = ExecutionRecord.model_validate_json(row["record_json"])
            if timestamp < current.updated_at:
                raise JournalConflictError(
                    "artifact timestamp precedes the current record"
                )
            existing = connection.execute(
                "SELECT content_text, content_hash FROM execution_artifacts "
                "WHERE execution_id = ? AND artifact_type = ?",
                (execution_id, artifact_type),
            ).fetchone()
            if existing is not None:
                if (
                    existing["content_text"] != content_text
                    or existing["content_hash"] != content_hash
                ):
                    raise JournalConflictError(
                        "artifact type already contains different immutable content"
                    )
                connection.commit()
                return False
            connection.execute(
                "INSERT INTO execution_artifacts "
                "(execution_id, artifact_type, content_text, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    execution_id,
                    artifact_type,
                    content_text,
                    content_hash,
                    self._timestamp(timestamp),
                ),
            )
            sequence, event_hash = self._append_event(
                connection,
                execution_id=execution_id,
                event_type="ARTIFACT_RECORDED",
                from_state=current.state,
                to_state=current.state,
                occurred_at=timestamp,
                payload={
                    "artifact_hash": content_hash,
                    "artifact_type": artifact_type,
                    "record_hash": self._record_hash(current),
                    "version": current.version,
                },
            )
            self._append_outbox_event(
                connection,
                record=current,
                event_type="EVIDENCE_RECORDED",
                sequence=sequence,
                event_hash=event_hash,
                occurred_at=timestamp,
                evidence_refs=(content_hash,),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_artifact(self, execution_id: str, artifact_type: str) -> tuple[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content_text, content_hash FROM execution_artifacts "
                "WHERE execution_id = ? AND artifact_type = ?",
                (execution_id, artifact_type),
            ).fetchone()
        if row is None:
            raise KeyError((execution_id, artifact_type))
        content = str(row["content_text"])
        content_hash = str(row["content_hash"])
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash:
            raise AuditIntegrityError("persisted artifact hash is invalid")
        return content, content_hash

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _event_material(
        *,
        execution_id: str,
        sequence: int,
        event_type: str,
        from_state: str | None,
        to_state: str,
        occurred_at: str,
        payload_json: str,
        previous_hash: str,
    ) -> dict[str, Any]:
        return {
            "execution_id": execution_id,
            "sequence": sequence,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "occurred_at": occurred_at,
            "payload": json.loads(payload_json),
            "previous_hash": previous_hash,
        }

    @staticmethod
    def _record_hash(record: ExecutionRecord) -> str:
        return canonical_sha256(json.loads(record.model_dump_json()))

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        execution_id: str,
        event_type: str,
        from_state: ExecutionState | None,
        to_state: ExecutionState,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> tuple[int, str]:
        last = connection.execute(
            "SELECT sequence, event_hash FROM audit_events WHERE execution_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        sequence = 0 if last is None else int(last["sequence"]) + 1
        previous_hash = GENESIS_HASH if last is None else str(last["event_hash"])
        occurred_text = self._timestamp(occurred_at)
        payload_json = canonical_json(payload)
        material = self._event_material(
            execution_id=execution_id,
            sequence=sequence,
            event_type=event_type,
            from_state=from_state.value if from_state else None,
            to_state=to_state.value,
            occurred_at=occurred_text,
            payload_json=payload_json,
            previous_hash=previous_hash,
        )
        event_hash = canonical_sha256(material)
        connection.execute(
            "INSERT INTO audit_events "
            "(execution_id, sequence, event_type, from_state, to_state, occurred_at, "
            "payload_json, previous_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                sequence,
                event_type,
                material["from_state"],
                material["to_state"],
                occurred_text,
                payload_json,
                previous_hash,
                event_hash,
            ),
        )
        return sequence, event_hash

    @staticmethod
    def _event_outcome(state: ExecutionState) -> ExecutionEventOutcome:
        if state == ExecutionState.SUCCEEDED:
            return ExecutionEventOutcome.SUCCEEDED
        if state == ExecutionState.REJECTED:
            return ExecutionEventOutcome.REJECTED
        if state == ExecutionState.ROLLED_BACK:
            return ExecutionEventOutcome.RECOVERED
        if state in {ExecutionState.FAILED, ExecutionState.ROLLBACK_FAILED}:
            return ExecutionEventOutcome.FAILED
        if state == ExecutionState.VERIFICATION_INCONCLUSIVE:
            return ExecutionEventOutcome.INCONCLUSIVE
        return ExecutionEventOutcome.IN_PROGRESS

    def _append_outbox_event(
        self,
        connection: sqlite3.Connection,
        *,
        record: ExecutionRecord,
        event_type: str,
        sequence: int,
        event_hash: str,
        occurred_at: datetime,
        evidence_refs: tuple[str, ...] = (),
    ) -> None:
        event = ExecutionEventV1(
            event_id=f"EVT-{event_hash}",
            event_type=event_type,
            execution_id=record.execution_id,
            operation_id=record.operation_id,
            correlation_id=record.correlation_id,
            trace_id=record.trace_id,
            idempotency_key=record.idempotency_key,
            state=record.state.value,
            outcome=self._event_outcome(record.state),
            evidence_refs=evidence_refs,
            occurred_at=occurred_at,
            audit_chain=AuditChainReference(sequence=sequence, event_hash=event_hash),
        )
        connection.execute(
            "INSERT INTO outbox_events "
            "(event_id, execution_id, audit_sequence, event_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.event_id,
                record.execution_id,
                sequence,
                event.model_dump_json(),
                self._timestamp(occurred_at),
            ),
        )
        outbox_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO outbox_delivery_state "
            "(outbox_id, delivery_id, state, attempt_count, updated_at) "
            "VALUES (?, ?, 'PENDING', 0, ?)",
            (
                outbox_id,
                f"DELIVERY-{event.event_id}",
                self._timestamp(occurred_at),
            ),
        )

    def create_execution(
        self,
        plan: DeploymentPlan,
        *,
        execution_id: str,
        approval_id: str,
        now: datetime | None = None,
    ) -> tuple[ExecutionRecord, bool]:
        """Create once by idempotency key and acquire the persistent workload lock."""
        timestamp = self._utc(now or datetime.now(UTC), "execution timestamp")
        if timestamp < plan.created_at:
            raise JournalConflictError("execution timestamp precedes plan creation")
        if timestamp >= plan.expires_at:
            raise JournalConflictError("deployment plan is expired")
        record = ExecutionRecord(
            execution_id=execution_id,
            operation_id=plan.operation_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            idempotency_key=plan.idempotency_key,
            correlation_id=plan.correlation_id,
            trace_id=plan.trace_id,
            target=plan.target,
            operation_type=plan.operation.operation_type,
            state=ExecutionState.RECEIVED,
            approval_id=approval_id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT record_json, plan_hash FROM executions WHERE idempotency_key = ?",
                (plan.idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["plan_hash"] != plan.plan_hash:
                    raise IdempotencyConflictError(
                        "idempotency key already belongs to a different plan"
                    )
                connection.commit()
                return ExecutionRecord.model_validate_json(
                    existing["record_json"]
                ), False
            try:
                connection.execute(
                    "INSERT INTO executions "
                    "(execution_id, operation_id, plan_id, plan_hash, idempotency_key, "
                    "workload_key, state, record_json, version, terminal, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        record.execution_id,
                        record.operation_id,
                        record.plan_id,
                        record.plan_hash,
                        record.idempotency_key,
                        record.target.workload_key,
                        record.state.value,
                        record.model_dump_json(),
                        record.version,
                        self._timestamp(timestamp),
                        self._timestamp(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                active = connection.execute(
                    "SELECT execution_id FROM executions WHERE workload_key = ? AND terminal = 0",
                    (record.target.workload_key,),
                ).fetchone()
                if active is not None:
                    raise WorkloadBusyError(
                        f"workload is locked by execution {active['execution_id']}"
                    ) from exc
                raise JournalConflictError(str(exc)) from exc
            sequence, event_hash = self._append_event(
                connection,
                execution_id=record.execution_id,
                event_type="EXECUTION_CREATED",
                from_state=None,
                to_state=record.state,
                occurred_at=timestamp,
                payload={
                    "plan_hash": record.plan_hash,
                    "record_hash": self._record_hash(record),
                    "version": record.version,
                },
            )
            self._append_outbox_event(
                connection,
                record=record,
                event_type="EXECUTION_CREATED",
                sequence=sequence,
                event_hash=event_hash,
                occurred_at=timestamp,
            )
            connection.commit()
            return record, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return ExecutionRecord.model_validate_json(row["record_json"])

    def transition(
        self,
        execution_id: str,
        target_state: ExecutionState,
        *,
        expected_version: int,
        reason: str | None = None,
        authorization_verifier_id: str | None = None,
        verification: RecoveryVerification | None = None,
        rollback_execution_id: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionRecord:
        timestamp = self._utc(now or datetime.now(UTC), "transition timestamp")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json, version FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            if int(row["version"]) != expected_version:
                raise ConcurrentUpdateError(
                    f"expected version {expected_version}, found {row['version']}"
                )
            current = ExecutionRecord.model_validate_json(row["record_json"])
            validate_transition(current.state, target_state)
            if timestamp < current.updated_at:
                raise JournalConflictError(
                    "transition timestamp precedes the current record"
                )
            if (
                target_state
                in {
                    ExecutionState.REJECTED,
                    ExecutionState.FAILED,
                    ExecutionState.ROLLBACK_FAILED,
                }
                and not reason
            ):
                raise JournalConflictError(f"{target_state.value} requires a reason")
            if (
                target_state == ExecutionState.AUTHORIZED
                and not authorization_verifier_id
            ):
                raise JournalConflictError(
                    "AUTHORIZED requires an authorization verifier ID"
                )
            if (
                target_state != ExecutionState.AUTHORIZED
                and authorization_verifier_id is not None
            ):
                raise JournalConflictError(
                    "authorization verifier ID may only be recorded on AUTHORIZED"
                )
            if verification is not None:
                if current.state not in {
                    ExecutionState.AWAITING_AGENT1,
                    ExecutionState.ROLLBACK_AWAITING_AGENT1,
                }:
                    raise JournalConflictError(
                        "recovery verification may only be recorded after AWAITING_AGENT1"
                    )
                if verification.execution_id != current.execution_id:
                    raise JournalConflictError(
                        "recovery verification belongs to a different execution"
                    )
                if verification.correlation_id != current.correlation_id:
                    raise JournalConflictError(
                        "recovery verification belongs to a different correlation"
                    )
                if verification.target_uid != current.target.uid:
                    raise JournalConflictError(
                        "recovery verification belongs to a different target"
                    )
                if verification.observed_at < current.updated_at:
                    raise JournalConflictError(
                        "recovery verification predates the Agent 1 verification request"
                    )
            if target_state == ExecutionState.VERIFICATION_INCONCLUSIVE and (
                verification is None
                or verification.verdict.value not in {"INCONCLUSIVE", "UNAVAILABLE"}
            ):
                raise JournalConflictError(
                    "VERIFICATION_INCONCLUSIVE requires INCONCLUSIVE or UNAVAILABLE evidence"
                )
            if (
                current.state == ExecutionState.AWAITING_AGENT1
                and target_state == ExecutionState.SUCCEEDED
                and (verification is None or verification.verdict.value != "VERIFIED")
            ):
                raise JournalConflictError(
                    "Agent 1 VERIFIED evidence is required for success"
                )
            if (
                current.state == ExecutionState.ROLLBACK_AWAITING_AGENT1
                and target_state == ExecutionState.ROLLED_BACK
                and (verification is None or verification.verdict.value != "VERIFIED")
            ):
                raise JournalConflictError(
                    "Agent 1 VERIFIED evidence is required for completed rollback"
                )
            if (
                current.state == ExecutionState.PLANNED
                and target_state == ExecutionState.SUCCEEDED
                and current.operation_type.value != "NO_OP"
            ):
                raise JournalConflictError(
                    "only NO_OP may succeed without execution and verification"
                )

            updated = current.model_copy(
                update={
                    "state": target_state,
                    "updated_at": timestamp,
                    "version": current.version + 1,
                    "failure_reason": reason or current.failure_reason,
                    "authorization_verifier_id": (
                        authorization_verifier_id or current.authorization_verifier_id
                    ),
                    "verification": verification or current.verification,
                    "rollback_execution_id": rollback_execution_id
                    or current.rollback_execution_id,
                }
            )
            # model_copy does not re-run validation in Pydantic v2.
            updated = ExecutionRecord.model_validate(updated.model_dump())
            cursor = connection.execute(
                "UPDATE executions SET state = ?, record_json = ?, version = ?, terminal = ?, "
                "updated_at = ? WHERE execution_id = ? AND version = ?",
                (
                    updated.state.value,
                    updated.model_dump_json(),
                    updated.version,
                    1 if is_terminal(updated.state) else 0,
                    self._timestamp(timestamp),
                    execution_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError("execution changed during transition")
            sequence, event_hash = self._append_event(
                connection,
                execution_id=execution_id,
                event_type="STATE_TRANSITION",
                from_state=current.state,
                to_state=target_state,
                occurred_at=timestamp,
                payload={
                    "authorization_verifier_id": updated.authorization_verifier_id,
                    "reason": reason,
                    "record_hash": self._record_hash(updated),
                    "rollback_execution_id": updated.rollback_execution_id,
                    "version": updated.version,
                    "verification_id": verification.verification_id
                    if verification
                    else None,
                    "verification_hash": (
                        canonical_sha256(verification) if verification else None
                    ),
                },
            )
            evidence_refs = (
                (canonical_sha256(verification),) if verification is not None else ()
            )
            self._append_outbox_event(
                connection,
                record=updated,
                event_type="STATE_TRANSITION",
                sequence=sequence,
                event_hash=event_hash,
                occurred_at=timestamp,
                evidence_refs=evidence_refs,
            )
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def audit_events(self, execution_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, event_type, from_state, to_state, occurred_at, payload_json, "
                "previous_hash, event_hash FROM audit_events WHERE execution_id = ? "
                "ORDER BY sequence",
                (execution_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_outbox(
        self, *, after_outbox_id: int = 0, limit: int = 100
    ) -> list[OutboxRecord]:
        """Read unpublished integration events without claiming delivery."""
        if after_outbox_id < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid outbox cursor or limit")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT outbox_id, event_json, created_at FROM outbox_events "
                "WHERE outbox_id > ? ORDER BY outbox_id LIMIT ?",
                (after_outbox_id, limit),
            ).fetchall()
        return [
            OutboxRecord(
                outbox_id=int(row["outbox_id"]),
                event=ExecutionEventV1.model_validate_json(row["event_json"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None

    def claim_outbox(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        max_attempts: int = 5,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[ClaimedOutboxDelivery]:
        """Claim pending events under a durable lease for at-least-once delivery."""
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and bounded")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not 1 <= max_attempts <= 100 or not 1 <= limit <= 100:
            raise ValueError("invalid outbox retry or batch bound")
        timestamp = self._utc(now or datetime.now(UTC), "outbox claim timestamp")
        lease_expires = timestamp + timedelta(seconds=lease_seconds)
        timestamp_text = self._timestamp(timestamp)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE outbox_delivery_state SET state = 'FAILED', lease_owner = NULL, "
                "lease_expires_at = NULL, last_error = ?, updated_at = ? "
                "WHERE state = 'CLAIMED' AND lease_expires_at <= ? "
                "AND attempt_count >= ?",
                (
                    "delivery lease expired after maximum attempts",
                    timestamp_text,
                    timestamp_text,
                    max_attempts,
                ),
            )
            rows = connection.execute(
                "SELECT d.outbox_id, d.delivery_id, d.attempt_count, o.event_json "
                "FROM outbox_delivery_state d JOIN outbox_events o "
                "ON o.outbox_id = d.outbox_id "
                "WHERE d.attempt_count < ? AND (d.state = 'PENDING' OR "
                "(d.state = 'CLAIMED' AND d.lease_expires_at <= ?)) "
                "ORDER BY d.outbox_id LIMIT ?",
                (max_attempts, timestamp_text, limit),
            ).fetchall()
            claimed: list[ClaimedOutboxDelivery] = []
            for row in rows:
                attempt = int(row["attempt_count"]) + 1
                cursor = connection.execute(
                    "UPDATE outbox_delivery_state SET state = 'CLAIMED', "
                    "attempt_count = ?, lease_owner = ?, lease_expires_at = ?, "
                    "last_error = NULL, updated_at = ? WHERE outbox_id = ? "
                    "AND attempt_count = ?",
                    (
                        attempt,
                        worker_id,
                        self._timestamp(lease_expires),
                        timestamp_text,
                        int(row["outbox_id"]),
                        int(row["attempt_count"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentUpdateError("outbox claim changed concurrently")
                claimed.append(
                    ClaimedOutboxDelivery(
                        outbox_id=int(row["outbox_id"]),
                        delivery_id=str(row["delivery_id"]),
                        event=ExecutionEventV1.model_validate_json(row["event_json"]),
                        attempt=attempt,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires,
                    )
                )
            connection.commit()
            return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acknowledge_outbox(
        self,
        delivery: ClaimedOutboxDelivery,
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = self._utc(now or datetime.now(UTC), "outbox acknowledgement timestamp")
        if timestamp >= delivery.lease_expires_at:
            raise ConcurrentUpdateError("outbox acknowledgement lease has expired")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE outbox_delivery_state SET state = 'DELIVERED', "
                "lease_owner = NULL, lease_expires_at = NULL, delivered_at = ?, "
                "updated_at = ? WHERE outbox_id = ? AND delivery_id = ? "
                "AND state = 'CLAIMED' AND lease_owner = ? AND attempt_count = ?",
                (
                    self._timestamp(timestamp),
                    self._timestamp(timestamp),
                    delivery.outbox_id,
                    delivery.delivery_id,
                    delivery.lease_owner,
                    delivery.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError("outbox acknowledgement lost its lease")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reject_outbox(
        self,
        delivery: ClaimedOutboxDelivery,
        *,
        error: str,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> OutboxDeliveryState:
        if not error:
            raise ValueError("delivery rejection requires an error")
        timestamp = self._utc(now or datetime.now(UTC), "outbox rejection timestamp")
        if timestamp >= delivery.lease_expires_at:
            raise ConcurrentUpdateError("outbox rejection lease has expired")
        state = (
            OutboxDeliveryState.FAILED
            if delivery.attempt >= max_attempts
            else OutboxDeliveryState.PENDING
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE outbox_delivery_state SET state = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, last_error = ?, updated_at = ? "
                "WHERE outbox_id = ? AND delivery_id = ? AND state = 'CLAIMED' "
                "AND lease_owner = ? AND attempt_count = ?",
                (
                    state.value,
                    error[:512],
                    self._timestamp(timestamp),
                    delivery.outbox_id,
                    delivery.delivery_id,
                    delivery.lease_owner,
                    delivery.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError("outbox rejection lost its lease")
            connection.commit()
            return state
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def outbox_delivery_statuses(self) -> list[OutboxDeliveryStatus]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT outbox_id, delivery_id, state, attempt_count, lease_owner, "
                "lease_expires_at, delivered_at, last_error "
                "FROM outbox_delivery_state ORDER BY outbox_id"
            ).fetchall()
        return [
            OutboxDeliveryStatus(
                outbox_id=int(row["outbox_id"]),
                delivery_id=str(row["delivery_id"]),
                state=OutboxDeliveryState(row["state"]),
                attempt_count=int(row["attempt_count"]),
                lease_owner=row["lease_owner"],
                lease_expires_at=self._parse_timestamp(row["lease_expires_at"]),
                delivered_at=self._parse_timestamp(row["delivered_at"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def verify_audit_chain(self, execution_id: str) -> bool:
        events = self.audit_events(execution_id)
        if not events:
            return False
        previous_hash = GENESIS_HASH
        for expected_sequence, event in enumerate(events):
            if (
                event["sequence"] != expected_sequence
                or event["previous_hash"] != previous_hash
            ):
                return False
            material = self._event_material(
                execution_id=execution_id,
                sequence=event["sequence"],
                event_type=event["event_type"],
                from_state=event["from_state"],
                to_state=event["to_state"],
                occurred_at=event["occurred_at"],
                payload_json=event["payload_json"],
                previous_hash=event["previous_hash"],
            )
            if canonical_sha256(material) != event["event_hash"]:
                return False
            previous_hash = event["event_hash"]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT operation_id, plan_id, plan_hash, idempotency_key, workload_key, "
                "state, record_json, version, terminal FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if row is None:
            return False
        try:
            record = ExecutionRecord.model_validate_json(row["record_json"])
            latest_payload = json.loads(events[-1]["payload_json"])
        except (TypeError, ValueError):
            return False
        expected_columns = {
            "operation_id": record.operation_id,
            "plan_id": record.plan_id,
            "plan_hash": record.plan_hash,
            "idempotency_key": record.idempotency_key,
            "workload_key": record.target.workload_key,
            "state": record.state.value,
            "version": record.version,
            "terminal": 1 if is_terminal(record.state) else 0,
        }
        if any(row[name] != value for name, value in expected_columns.items()):
            return False
        if latest_payload.get("record_hash") != self._record_hash(record):
            return False
        with self._connect() as connection:
            artifacts = connection.execute(
                "SELECT artifact_type, content_text, content_hash FROM execution_artifacts "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchall()
        if not all(
            hashlib.sha256(str(item["content_text"]).encode("utf-8")).hexdigest()
            == item["content_hash"]
            for item in artifacts
        ):
            return False
        recorded_artifacts = {
            (payload.get("artifact_type"), payload.get("artifact_hash"))
            for event in events
            if event["event_type"] == "ARTIFACT_RECORDED"
            for payload in (json.loads(event["payload_json"]),)
        }
        persisted_artifacts = {
            (str(item["artifact_type"]), str(item["content_hash"]))
            for item in artifacts
        }
        if recorded_artifacts != persisted_artifacts:
            return False
        with self._connect() as connection:
            outbox = connection.execute(
                "SELECT o.outbox_id, o.event_id, o.audit_sequence, o.event_json, "
                "d.delivery_id, d.state, d.attempt_count FROM outbox_events o "
                "LEFT JOIN outbox_delivery_state d ON d.outbox_id = o.outbox_id "
                "WHERE o.execution_id = ? ORDER BY o.audit_sequence",
                (execution_id,),
            ).fetchall()
        if len(outbox) != len(events):
            return False
        try:
            for event, outbox_row in zip(events, outbox, strict=True):
                integration = ExecutionEventV1.model_validate_json(
                    outbox_row["event_json"]
                )
                if (
                    outbox_row["delivery_id"]
                    != f"DELIVERY-{outbox_row['event_id']}"
                    or outbox_row["state"] not in {
                        item.value for item in OutboxDeliveryState
                    }
                    or int(outbox_row["attempt_count"]) < 0
                    or int(outbox_row["audit_sequence"]) != event["sequence"]
                    or integration.audit_chain.sequence != event["sequence"]
                    or integration.audit_chain.event_hash != event["event_hash"]
                    or integration.event_id != f"EVT-{event['event_hash']}"
                    or integration.execution_id != execution_id
                    or integration.operation_id != record.operation_id
                    or integration.correlation_id != record.correlation_id
                    or integration.trace_id != record.trace_id
                    or integration.idempotency_key != record.idempotency_key
                    or integration.state != event["to_state"]
                ):
                    return False
        except (TypeError, ValueError):
            return False
        return True

    def assert_audit_integrity(self, execution_id: str) -> None:
        if not self.verify_audit_chain(execution_id):
            raise AuditIntegrityError(f"invalid audit chain for {execution_id}")
