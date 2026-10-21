import hashlib
import sqlite3

import pytest

from agent4_deployment_recovery.core.errors import (
    AuditIntegrityError,
    JournalConflictError,
)
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


def test_artifacts_are_hash_checked_idempotent_and_append_only(
    tmp_path, operation_factory
):
    database = tmp_path / "journal.db"
    journal = SQLiteExecutionJournal(database)
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-artifact", approval_id="APR-001"
    )
    content = '{"exact":"bytes"}'
    digest = hashlib.sha256(content.encode()).hexdigest()
    assert (
        journal.record_artifact(record.execution_id, "patch", content, digest) is True
    )
    assert (
        journal.record_artifact(record.execution_id, "patch", content, digest) is False
    )
    assert journal.get_artifact(record.execution_id, "patch") == (content, digest)
    assert journal.verify_audit_chain(record.execution_id) is True

    with pytest.raises(JournalConflictError, match="different immutable"):
        other = '{"exact":"different"}'
        journal.record_artifact(
            record.execution_id,
            "patch",
            other,
            hashlib.sha256(other.encode()).hexdigest(),
        )
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "UPDATE execution_artifacts SET content_text = '{}' WHERE execution_id = ?",
            (record.execution_id,),
        )


def test_artifact_hash_mismatch_and_database_tampering_fail_closed(
    tmp_path, operation_factory
):
    database = tmp_path / "journal.db"
    journal = SQLiteExecutionJournal(database)
    plan = create_plan(operation_factory())
    record, _ = journal.create_execution(
        plan, execution_id="EXEC-artifact", approval_id="APR-001"
    )
    with pytest.raises(JournalConflictError, match="hash"):
        journal.record_artifact(record.execution_id, "patch", "payload", "0" * 64)

    content = "payload"
    digest = hashlib.sha256(content.encode()).hexdigest()
    journal.record_artifact(record.execution_id, "patch", content, digest)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER execution_artifacts_no_update")
        connection.execute(
            "UPDATE execution_artifacts SET content_text = 'tampered' WHERE execution_id = ?",
            (record.execution_id,),
        )
    with pytest.raises(AuditIntegrityError):
        journal.get_artifact(record.execution_id, "patch")
    assert journal.verify_audit_chain(record.execution_id) is False
