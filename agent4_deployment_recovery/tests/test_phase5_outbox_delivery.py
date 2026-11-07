from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from agent4_deployment_recovery.adapters.outbox import (
    InMemoryOutboxTransport,
    OutboxDispatcher,
)
from agent4_deployment_recovery.core.errors import ConcurrentUpdateError
from agent4_deployment_recovery.core.integration_models import OutboxDeliveryState
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


def _journal_with_event(tmp_path, operation_factory):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    journal.create_execution(plan, execution_id="EXEC-outbox", approval_id="APR-001")
    return journal


def test_dispatch_acknowledges_and_never_redelivers_completed_event(
    tmp_path, operation_factory
):
    journal = _journal_with_event(tmp_path, operation_factory)
    transport = InMemoryOutboxTransport()
    dispatcher = OutboxDispatcher(
        journal=journal, transport=transport, worker_id="worker-one"
    )
    assert dispatcher.dispatch_once() == (1, 0)
    assert dispatcher.dispatch_once() == (0, 0)
    status = journal.outbox_delivery_statuses()[0]
    assert status.state == OutboxDeliveryState.DELIVERED
    assert status.attempt_count == 1


def test_failure_retries_are_bounded(tmp_path, operation_factory):
    journal = _journal_with_event(tmp_path, operation_factory)
    transport = InMemoryOutboxTransport(fail=lambda delivery: True)
    dispatcher = OutboxDispatcher(
        journal=journal,
        transport=transport,
        worker_id="worker-one",
        max_attempts=2,
    )
    assert dispatcher.dispatch_once() == (0, 1)
    assert dispatcher.dispatch_once() == (0, 1)
    assert dispatcher.dispatch_once() == (0, 0)
    status = journal.outbox_delivery_statuses()[0]
    assert status.state == OutboxDeliveryState.FAILED
    assert status.attempt_count == 2


def test_concurrent_dispatchers_cannot_share_an_active_lease(
    tmp_path, operation_factory
):
    journal = _journal_with_event(tmp_path, operation_factory)

    def claim(worker):
        separate_process = SQLiteExecutionJournal(journal.path)
        return separate_process.claim_outbox(worker_id=worker, limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-one", "worker-two")))
    assert sorted(len(item) for item in claims) == [0, 1]


def test_crash_after_delivery_reuses_delivery_id_after_lease_expiry(
    tmp_path, operation_factory
):
    journal = _journal_with_event(tmp_path, operation_factory)
    start = datetime.now(UTC)
    first = journal.claim_outbox(
        worker_id="crashed-worker", lease_seconds=5, now=start
    )[0]
    # A transport may have accepted this event before the worker crashed.
    assert journal.claim_outbox(
        worker_id="early-worker", lease_seconds=5, now=start + timedelta(seconds=4)
    ) == []
    retried = journal.claim_outbox(
        worker_id="recovery-worker", lease_seconds=5, now=start + timedelta(seconds=6)
    )[0]
    assert retried.delivery_id == first.delivery_id
    assert retried.attempt == 2
    journal.acknowledge_outbox(retried, now=start + timedelta(seconds=7))


def test_delivery_state_is_created_atomically_with_outbox(
    tmp_path, operation_factory, monkeypatch
):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())

    original = journal._append_outbox_event

    def fail_after_outbox(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before transaction commit")

    monkeypatch.setattr(journal, "_append_outbox_event", fail_after_outbox)
    with pytest.raises(RuntimeError, match="transaction"):
        journal.create_execution(
            plan, execution_id="EXEC-atomic-outbox", approval_id="APR-001"
        )
    assert journal.read_outbox() == []
    assert journal.outbox_delivery_statuses() == []


def test_expired_worker_cannot_acknowledge_or_reject_lease(
    tmp_path, operation_factory
):
    journal = _journal_with_event(tmp_path, operation_factory)
    start = datetime.now(UTC)
    claim = journal.claim_outbox(
        worker_id="expired-worker", lease_seconds=1, now=start
    )[0]
    with pytest.raises(ConcurrentUpdateError, match="expired"):
        journal.acknowledge_outbox(claim, now=start + timedelta(seconds=1))
    with pytest.raises(ConcurrentUpdateError, match="expired"):
        journal.reject_outbox(
            claim,
            error="late response",
            now=start + timedelta(seconds=1),
        )


def test_audit_integrity_detects_outbox_delivery_identity_tampering(
    tmp_path, operation_factory
):
    journal = _journal_with_event(tmp_path, operation_factory)
    assert journal.verify_audit_chain("EXEC-outbox") is True
    with journal._connect() as connection:
        connection.execute(
            "UPDATE outbox_delivery_state SET delivery_id = 'DELIVERY-tampered'"
        )
    assert journal.verify_audit_chain("EXEC-outbox") is False
