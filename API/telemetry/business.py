"""Application transaction database and immutable, externally verified outcome inbox."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from API.telemetry.contracts import PredictionEvent, Scope, utc_text
from API.telemetry.operations import FEATURES


class BusinessOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    observation_id: UUID
    model_version: Annotated[str, Field(min_length=1, max_length=160)]
    actual: Annotated[int, Field(strict=True, ge=0, le=1)]
    evidence_ref: Annotated[str, Field(min_length=1, max_length=160)]
    occurred_at: AwareDatetime


class BusinessOutcomeConflict(ValueError):
    pass


def initialize_business(connection: sqlite3.Connection) -> None:
    from API.telemetry.business_events import initialize_events

    initialize_events(connection)
    connection.execute("""CREATE TABLE IF NOT EXISTS transactions (
        observation_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,project_id TEXT NOT NULL,
        model_id TEXT NOT NULL,model_version TEXT NOT NULL,predicted INTEGER NOT NULL
        CHECK(predicted IN(0,1)),observed_at TEXT NOT NULL,inputs_json TEXT NOT NULL,
        channel TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS resolved_outcomes (
        sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,
        observation_id TEXT NOT NULL UNIQUE REFERENCES transactions(observation_id),
        model_version TEXT NOT NULL,actual INTEGER NOT NULL CHECK(actual IN(0,1)),
        evidence_ref TEXT NOT NULL,occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,
        tenant_id TEXT NOT NULL,project_id TEXT NOT NULL,model_id TEXT NOT NULL
    )""")
    connection.execute("""CREATE VIEW IF NOT EXISTS agent6_business_outcomes_v1 AS
        SELECT sequence_id,event_id,observation_id,model_version,actual,evidence_ref,
               occurred_at,tenant_id,project_id,model_id FROM resolved_outcomes
    """)
    # Preserve the identity of older captured predictions without inventing missing inputs.
    connection.execute("""INSERT OR IGNORE INTO transactions
        SELECT observation_id,tenant_id,project_id,model_id,model_version,
               json_extract(CAST(payload AS TEXT),'$.predicted'),observed_at,'{}','legacy'
        FROM predictions WHERE observation_id NOT IN (SELECT observation_id FROM transactions)
    """)


def insert_transaction(
    connection: sqlite3.Connection,
    event: PredictionEvent,
    inputs: dict[str, object],
    channel: str,
) -> None:
    raw = feature_json(inputs)
    connection.execute(
        "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?)",
        (
            event.observation_id,
            event.tenant_id,
            event.project_id,
            event.model_id,
            event.model_version,
            event.predicted,
            utc_text(event.observed_at),
            raw,
            channel,
        ),
    )


def feature_json(inputs: dict[str, object]) -> str:
    """Canonical allowlisted model features, shared by storage and evidence digests."""
    allowed = {name: inputs[name] for name in FEATURES if name in inputs}
    raw = json.dumps(allowed, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > 16384:
        raise ValueError("Transaction input exceeds the database record limit")
    return raw


def resolve_outcome(
    connection: sqlite3.Connection,
    scope: Scope,
    event: BusinessOutcome,
    *,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    transaction = connection.execute(
        "SELECT model_version,observed_at FROM transactions "
        "WHERE observation_id=? AND tenant_id=? AND project_id=? AND model_id=?",
        (str(event.observation_id), scope.tenant_id, scope.project_id, scope.model_id),
    ).fetchone()
    if transaction is None or transaction[0] != event.model_version:
        raise LookupError(
            "Transaction and exact model version were not found in this scope"
        )
    observed = datetime.fromisoformat(transaction[1])
    if not observed <= event.occurred_at <= now:
        raise ValueError(
            "Verified outcome must follow its transaction and cannot be future-dated"
        )
    values = (
        str(event.event_id),
        str(event.observation_id),
        event.model_version,
        event.actual,
        event.evidence_ref,
        utc_text(event.occurred_at),
    )
    existing = connection.execute(
        "SELECT event_id,observation_id,model_version,actual,evidence_ref,"
        "occurred_at,sequence_id FROM resolved_outcomes WHERE event_id=? OR observation_id=?",
        (str(event.event_id), str(event.observation_id)),
    ).fetchall()
    if existing:
        if len(existing) != 1 or tuple(existing[0][:6]) != values:
            raise BusinessOutcomeConflict(
                "Verified outcomes are immutable; conflicting event rejected"
            )
        return int(existing[0][6])
    result = connection.execute(
        "INSERT INTO resolved_outcomes "
        "(event_id,observation_id,model_version,actual,evidence_ref,occurred_at,received_at,"
        "tenant_id,project_id,model_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        values + (utc_text(now), scope.tenant_id, scope.project_id, scope.model_id),
    )
    if result.lastrowid is None:
        raise sqlite3.IntegrityError("Verified outcome was not assigned a sequence")
    return int(result.lastrowid)
