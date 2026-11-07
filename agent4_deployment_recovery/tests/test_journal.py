import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from agent4_deployment_recovery.core.errors import (
    AuditIntegrityError,
    ConcurrentUpdateError,
    IdempotencyConflictError,
    JournalConflictError,
    WorkloadBusyError,
)
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import (
    ExecutionState,
    NoOp,
    OperationType,
    RecoveryVerification,
    VerificationVerdict,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


def _advance_to_agent1(journal, record):
    for state, kwargs in (
        (ExecutionState.AUTHORIZED, {"authorization_verifier_id": "test-verifier"}),
        (ExecutionState.PREFLIGHT_PASSED, {}),
        (ExecutionState.PLANNED, {}),
        (ExecutionState.DRY_RUN_PASSED, {}),
        (ExecutionState.EXECUTING, {}),
        (ExecutionState.OBSERVING, {}),
        (ExecutionState.AWAITING_AGENT1, {}),
    ):
        record = journal.transition(
            record.execution_id,
            state,
            expected_version=record.version,
            **kwargs,
        )
    return record


def test_idempotent_creation_returns_original_record(tmp_path, operation_factory):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    first, created = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )
    replay, replay_created = journal.create_execution(
        plan, execution_id="EXEC-ignored", approval_id="APR-001"
    )
    assert created is True
    assert replay_created is False
    assert replay.execution_id == first.execution_id


def test_idempotency_key_cannot_cover_different_plan(tmp_path, operation_factory):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    first = create_plan(operation_factory(suffix="001", idempotency_key="IDEMP-shared"))
    second = create_plan(
        operation_factory(suffix="002", idempotency_key="IDEMP-shared")
    )
    journal.create_execution(first, execution_id="EXEC-001", approval_id="APR-001")
    with pytest.raises(IdempotencyConflictError):
        journal.create_execution(second, execution_id="EXEC-002", approval_id="APR-002")


def test_persistent_workload_lock_and_terminal_release(tmp_path, operation_factory):
    db_path = tmp_path / "journal.db"
    first_journal = SQLiteExecutionJournal(db_path)
    second_journal = SQLiteExecutionJournal(db_path)
    first = create_plan(operation_factory(suffix="001"))
    second = create_plan(operation_factory(suffix="002"))
    record, _ = first_journal.create_execution(
        first, execution_id="EXEC-001", approval_id="APR-001"
    )
    with pytest.raises(WorkloadBusyError):
        second_journal.create_execution(
            second, execution_id="EXEC-002", approval_id="APR-002"
        )
    record = first_journal.transition(
        record.execution_id,
        ExecutionState.REJECTED,
        expected_version=record.version,
        reason="test rejection releases the workload",
    )
    assert record.state == ExecutionState.REJECTED
    _, created = second_journal.create_execution(
        second, execution_id="EXEC-002", approval_id="APR-002"
    )
    assert created is True


def test_concurrent_workload_acquisition_has_exactly_one_winner(
    tmp_path, operation_factory
):
    db_path = tmp_path / "journal.db"
    journals = (SQLiteExecutionJournal(db_path), SQLiteExecutionJournal(db_path))
    plans = (
        create_plan(operation_factory(suffix="001")),
        create_plan(operation_factory(suffix="002")),
    )
    barrier = Barrier(2)

    def acquire(index):
        barrier.wait()
        try:
            journals[index].create_execution(
                plans[index],
                execution_id=f"EXEC-00{index + 1}",
                approval_id=f"APR-00{index + 1}",
            )
        except WorkloadBusyError:
            return "busy"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, range(2)))

    assert sorted(results) == ["busy", "created"]


def test_expired_plan_and_naive_journal_timestamps_fail_closed(
    tmp_path, operation_factory
):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    with pytest.raises(JournalConflictError, match="expired"):
        journal.create_execution(
            plan,
            execution_id="EXEC-expired",
            approval_id="APR-001",
            now=plan.expires_at,
        )
    with pytest.raises(JournalConflictError, match="timezone-aware"):
        journal.create_execution(
            plan,
            execution_id="EXEC-naive",
            approval_id="APR-001",
            now=plan.created_at.replace(tzinfo=None),
        )


def test_transition_rejects_non_monotonic_timestamp(tmp_path, operation_factory):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    created_at = plan.created_at + timedelta(seconds=1)
    record, _ = journal.create_execution(
        plan,
        execution_id="EXEC-001",
        approval_id="APR-001",
        now=created_at,
    )
    record = journal.transition(
        record.execution_id,
        ExecutionState.AUTHORIZED,
        expected_version=record.version,
        authorization_verifier_id="test-verifier",
        now=created_at + timedelta(seconds=10),
    )
    with pytest.raises(JournalConflictError, match="precedes"):
        journal.transition(
            record.execution_id,
            ExecutionState.PREFLIGHT_PASSED,
            expected_version=record.version,
            now=created_at + timedelta(seconds=5),
        )


def test_failed_transition_rolls_back_record_and_audit_event(
    tmp_path, operation_factory, monkeypatch
):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )

    def fail_to_append(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated audit write failure")

    monkeypatch.setattr(journal, "_append_event", fail_to_append)
    with pytest.raises(RuntimeError, match="audit write"):
        journal.transition(
            record.execution_id,
            ExecutionState.AUTHORIZED,
            expected_version=record.version,
            authorization_verifier_id="test-verifier",
        )

    persisted = SQLiteExecutionJournal(tmp_path / "journal.db").get_execution(
        record.execution_id
    )
    assert persisted.state == ExecutionState.RECEIVED
    assert persisted.version == 1
    assert len(journal.audit_events(record.execution_id)) == 1


def test_optimistic_version_and_audit_chain(tmp_path, operation_factory):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )
    updated = journal.transition(
        record.execution_id,
        ExecutionState.AUTHORIZED,
        expected_version=1,
        authorization_verifier_id="isolated-test-verifier",
    )
    assert updated.version == 2
    assert journal.verify_audit_chain(record.execution_id) is True
    with pytest.raises(ConcurrentUpdateError):
        journal.transition(
            record.execution_id,
            ExecutionState.PREFLIGHT_PASSED,
            expected_version=1,
        )


def test_audit_rows_are_append_only(tmp_path, operation_factory):
    db_path = tmp_path / "journal.db"
    journal = SQLiteExecutionJournal(db_path)
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )
    with sqlite3.connect(db_path) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "UPDATE audit_events SET payload_json = '{}' WHERE execution_id = ?",
            (record.execution_id,),
        )
    assert journal.verify_audit_chain(record.execution_id) is True


def test_audit_integrity_detects_execution_record_tampering(
    tmp_path, operation_factory
):
    db_path = tmp_path / "journal.db"
    journal = SQLiteExecutionJournal(db_path)
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )
    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT record_json FROM executions WHERE execution_id = ?",
            (record.execution_id,),
        ).fetchone()[0]
        payload = json.loads(stored)
        payload["approval_id"] = "APR-tampered"
        connection.execute(
            "UPDATE executions SET record_json = ? WHERE execution_id = ?",
            (json.dumps(payload), record.execution_id),
        )

    assert journal.verify_audit_chain(record.execution_id) is False
    with pytest.raises(AuditIntegrityError):
        journal.assert_audit_integrity(record.execution_id)


def test_mutation_success_requires_agent1_verified_evidence(
    tmp_path, operation_factory
):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )
    record = _advance_to_agent1(journal, record)
    with pytest.raises(JournalConflictError, match="VERIFIED"):
        journal.transition(
            record.execution_id,
            ExecutionState.SUCCEEDED,
            expected_version=record.version,
        )
    verification = RecoveryVerification(
        verification_id="VERIFY-001",
        execution_id=record.execution_id,
        correlation_id=record.correlation_id,
        verifier_id="trusted-agent1-test-verifier",
        verdict=VerificationVerdict.VERIFIED,
        observed_at=datetime.now(UTC),
        target_uid=record.target.uid,
        detail="isolated test verification",
        signature="test-signature",
    )
    record = journal.transition(
        record.execution_id,
        ExecutionState.SUCCEEDED,
        expected_version=record.version,
        verification=verification,
    )
    assert record.state == ExecutionState.SUCCEEDED


def test_agent1_evidence_must_match_correlation_and_follow_request(
    tmp_path, operation_factory
):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-001", approval_id="APR-001"
    )
    record = _advance_to_agent1(journal, record)
    common = {
        "verification_id": "VERIFY-001",
        "execution_id": record.execution_id,
        "verifier_id": "trusted-agent1-test-verifier",
        "verdict": VerificationVerdict.VERIFIED,
        "target_uid": record.target.uid,
        "detail": "isolated test verification",
        "signature": "test-signature",
    }
    wrong_correlation = RecoveryVerification(
        **common,
        correlation_id="CORR-unrelated",
        observed_at=datetime.now(UTC),
    )
    with pytest.raises(JournalConflictError, match="correlation"):
        journal.transition(
            record.execution_id,
            ExecutionState.SUCCEEDED,
            expected_version=record.version,
            verification=wrong_correlation,
        )

    stale = RecoveryVerification(
        **common,
        correlation_id=record.correlation_id,
        observed_at=record.updated_at - timedelta(microseconds=1),
    )
    with pytest.raises(JournalConflictError, match="predates"):
        journal.transition(
            record.execution_id,
            ExecutionState.SUCCEEDED,
            expected_version=record.version,
            verification=stale,
        )


def test_noop_may_complete_without_mutation_or_agent1(tmp_path, operation_factory):
    operation = NoOp(operation_type=OperationType.NO_OP, reason="nothing to change")
    plan = create_plan(operation_factory(operation=operation))
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-noop", approval_id="APR-001"
    )
    for state, kwargs in (
        (ExecutionState.AUTHORIZED, {"authorization_verifier_id": "test-verifier"}),
        (ExecutionState.PREFLIGHT_PASSED, {}),
        (ExecutionState.PLANNED, {}),
        (ExecutionState.SUCCEEDED, {}),
    ):
        record = journal.transition(
            record.execution_id,
            state,
            expected_version=record.version,
            **kwargs,
        )
    assert record.state == ExecutionState.SUCCEEDED
