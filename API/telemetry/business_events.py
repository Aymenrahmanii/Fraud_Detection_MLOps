"""Durable final business confirmations correlated without model observation IDs."""

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from API.telemetry.business import (
    BusinessOutcome,
    BusinessOutcomeConflict,
    resolve_outcome,
)
from API.telemetry.contracts import PredictionEvent, Scope

TransactionId = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]
TRANSACTION_ID = TypeAdapter(TransactionId)


class BusinessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    transaction_id: TransactionId
    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: Literal["FRAUD_CONFIRMED", "LEGITIMATE_CONFIRMED"]
    verification_status: Literal["VERIFIED"]
    evidence_ref: Annotated[str, Field(min_length=3, max_length=160)]
    occurred_at: AwareDatetime


def initialize_events(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS agent6_transaction_bindings_v1 (
        observation_id TEXT PRIMARY KEY REFERENCES predictions(observation_id),
        tenant_id TEXT NOT NULL,project_id TEXT NOT NULL,model_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL,input_ref TEXT NOT NULL
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS agent6_business_transaction_v1
        ON agent6_transaction_bindings_v1(tenant_id,project_id,model_id,transaction_id)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent6_business_events_v1 (
        event_id TEXT NOT NULL,tenant_id TEXT NOT NULL,project_id TEXT NOT NULL,
        model_id TEXT NOT NULL,transaction_id TEXT NOT NULL,payload BLOB NOT NULL,
        digest TEXT NOT NULL,received_at TEXT NOT NULL,
        PRIMARY KEY(tenant_id,project_id,model_id,event_id),
        UNIQUE(tenant_id,project_id,model_id,transaction_id)
    )""")


def reconcile(connection: sqlite3.Connection, scope: Scope, transaction_id: str) -> int:
    row = connection.execute(
        "SELECT payload,digest FROM agent6_business_events_v1 WHERE tenant_id=? "
        "AND project_id=? AND model_id=? AND transaction_id=?",
        (scope.tenant_id, scope.project_id, scope.model_id, transaction_id),
    ).fetchone()
    if row is None:
        return 0
    raw, digest = row
    if sha256(raw).hexdigest() != digest:
        raise ValueError("Business event integrity differs")
    event = BusinessEvent.model_validate_json(raw)
    if event.transaction_id != transaction_id:
        raise ValueError("Business event index differs from its evidence")
    observations = connection.execute(
        "SELECT p.observation_id,p.model_version,p.observed_at FROM predictions p "
        "JOIN agent6_transaction_bindings_v1 b ON b.observation_id=p.observation_id "
        "WHERE b.tenant_id=? AND b.project_id=? AND b.model_id=? AND b.transaction_id=? LIMIT 1001",
        (scope.tenant_id, scope.project_id, scope.model_id, transaction_id),
    ).fetchall()
    if len(observations) > 1000:
        raise ValueError(
            "Business transaction exceeds the observation correlation bound"
        )
    matched = 0
    for identity, version, observed in observations:
        # An already known answer must not become a label for a later prediction.
        if datetime.fromisoformat(observed) > event.occurred_at:
            continue
        resolve_outcome(
            connection,
            scope,
            BusinessOutcome(
                event_id=uuid5(
                    NAMESPACE_URL,
                    f"{scope.tenant_id}:{scope.project_id}:{scope.model_id}:{event.event_id}:{identity}",
                ),
                observation_id=UUID(identity),
                model_version=version,
                actual=int(event.status == "FRAUD_CONFIRMED"),
                evidence_ref=event.evidence_ref,
                occurred_at=event.occurred_at,
            ),
        )
        matched += 1
    return matched


def bind_transaction(
    connection: sqlite3.Connection,
    scope: Scope,
    prediction: PredictionEvent,
    transaction_id: str,
) -> None:
    transaction_id = TRANSACTION_ID.validate_python(transaction_id)
    if prediction.input_ref is None:
        raise ValueError("Business correlation requires captured input evidence")
    raw_inputs = connection.execute(
        "SELECT inputs_json FROM transactions WHERE observation_id=?",
        (prediction.observation_id,),
    ).fetchone()
    if (
        raw_inputs is None
        or "sha256:" + sha256(raw_inputs[0].encode()).hexdigest()
        != prediction.input_ref
    ):
        raise ValueError("Business binding input evidence differs")
    # API float coercion and Streamlit integer widgets describe the same numeric values.
    normalized = {
        key: float(value) if type(value) in (int, float) else value
        for key, value in json.loads(raw_inputs[0]).items()
    }
    business_input_ref = (
        "sha256:"
        + sha256(
            json.dumps(
                normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )
    existing = connection.execute(
        "SELECT input_ref FROM agent6_transaction_bindings_v1 WHERE tenant_id=? "
        "AND project_id=? AND model_id=? AND transaction_id=? LIMIT 1",
        (scope.tenant_id, scope.project_id, scope.model_id, transaction_id),
    ).fetchone()
    if existing and existing[0] != business_input_ref:
        raise BusinessOutcomeConflict(
            "A business transaction cannot be rebound to different inputs"
        )
    connection.execute(
        "INSERT INTO agent6_transaction_bindings_v1 VALUES(?,?,?,?,?,?)",
        (
            prediction.observation_id,
            scope.tenant_id,
            scope.project_id,
            scope.model_id,
            transaction_id,
            business_input_ref,
        ),
    )
    reconcile(connection, scope, transaction_id)


def receive_event(
    connection: sqlite3.Connection,
    scope: Scope,
    event: BusinessEvent,
    allowed_sources: tuple[str, ...],
) -> dict[str, object]:
    if event.source_id not in allowed_sources:
        raise ValueError("Business publisher is not allowlisted")
    now = datetime.now(UTC)
    if event.occurred_at > now:
        raise ValueError("Business confirmations cannot be future-dated")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    raw = json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    digest = sha256(raw).hexdigest()
    rows = connection.execute(
        "SELECT payload,digest FROM agent6_business_events_v1 WHERE tenant_id=? "
        "AND project_id=? AND model_id=? AND (event_id=? OR transaction_id=?)",
        (
            scope.tenant_id,
            scope.project_id,
            scope.model_id,
            str(event.event_id),
            event.transaction_id,
        ),
    ).fetchall()
    if rows and (len(rows) != 1 or tuple(rows[0]) != (raw, digest)):
        raise BusinessOutcomeConflict(
            "Conflicting business confirmation rejected; existing evidence retained"
        )
    connection.execute(
        "INSERT OR IGNORE INTO agent6_business_events_v1 VALUES(?,?,?,?,?,?,?,?)",
        (
            str(event.event_id),
            scope.tenant_id,
            scope.project_id,
            scope.model_id,
            event.transaction_id,
            raw,
            digest,
            now.isoformat(),
        ),
    )
    matched = reconcile(connection, scope, event.transaction_id)
    return {
        "status": "RESOLVED" if matched else "WAITING_FOR_PREDICTION",
        "event_id": str(event.event_id),
        "matched_observations": matched,
        "replayed": bool(rows),
    }
