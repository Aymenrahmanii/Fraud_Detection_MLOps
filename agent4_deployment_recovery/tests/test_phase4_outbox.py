import pytest

from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.schemas import ExecutionState
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


def test_outbox_is_hash_linked_and_survives_process_restart(
    tmp_path, operation_factory
):
    path = tmp_path / "journal.db"
    journal = SQLiteExecutionJournal(path)
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-outbox", approval_id="APR-001"
    )
    record = journal.transition(
        record.execution_id,
        ExecutionState.AUTHORIZED,
        expected_version=record.version,
        authorization_verifier_id="trusted-verifier",
    )
    first = journal.read_outbox()
    restarted = SQLiteExecutionJournal(path)
    second = restarted.read_outbox()
    assert first == second
    assert [item.event.state for item in second] == ["RECEIVED", "AUTHORIZED"]
    assert all(
        item.event.audit_chain.event_hash
        == restarted.audit_events(record.execution_id)[index]["event_hash"]
        for index, item in enumerate(second)
    )
    assert restarted.verify_audit_chain(record.execution_id) is True


def test_outbox_failure_rolls_back_state_and_audit_atomically(
    tmp_path, operation_factory, monkeypatch
):
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-atomic", approval_id="APR-001"
    )
    before_events = journal.audit_events(record.execution_id)
    before_outbox = journal.read_outbox()

    def fail_outbox(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(journal, "_append_outbox_event", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox"):
        journal.transition(
            record.execution_id,
            ExecutionState.AUTHORIZED,
            expected_version=record.version,
            authorization_verifier_id="trusted-verifier",
        )
    assert journal.get_execution(record.execution_id) == record
    assert journal.audit_events(record.execution_id) == before_events
    assert journal.read_outbox() == before_outbox
