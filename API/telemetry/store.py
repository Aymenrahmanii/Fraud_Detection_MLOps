"""Transactional local prediction log; HTTP success follows a durable append."""

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from API.telemetry.contracts import PredictionEvent, Scope, utc_text

APPLICATION_ID = 0x41365052
SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 4096


class PredictionLogError(RuntimeError):
    """A prediction could not be durably recorded or verified."""


def canonical_event(event: PredictionEvent) -> bytes:
    return json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def verify_database(connection: sqlite3.Connection) -> None:
    if (
        connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
    ):
        raise PredictionLogError("Not a supported serving prediction database")


class SQLitePredictionLog:
    def __init__(
        self,
        database: Path,
        scope: Scope,
        model_sha256: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if str(database) == ":memory:" or not re.fullmatch(
            r"[0-9a-f]{64}", model_sha256
        ):
            raise ValueError(
                "Durable storage and the full serving-artifact digest are required"
            )
        self.database = database.resolve()
        self.scope = Scope.model_validate_json(scope.model_dump_json())
        self.model_sha256 = model_sha256
        self.model_version = "artifact-" + model_sha256
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                app_id = connection.execute("PRAGMA application_id").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if app_id == 0 and version == 0 and not tables:
                    connection.execute("""CREATE TABLE predictions (
                        observation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                        project_id TEXT NOT NULL, model_id TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        observed_at TEXT NOT NULL, payload BLOB NOT NULL,
                        digest TEXT NOT NULL
                    )""")
                    connection.execute("""CREATE INDEX prediction_scope_window
                        ON predictions (tenant_id, project_id, model_id,
                        model_version, observed_at)""")
                    connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                else:
                    verify_database(connection)
                from API.telemetry.business import initialize_business

                initialize_business(connection)
        except OSError as error:
            raise PredictionLogError(
                "Serving prediction storage is unavailable"
            ) from error

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.database, timeout=5)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                with connection:
                    yield connection
        except sqlite3.Error as error:
            raise PredictionLogError(
                "Serving prediction storage is unavailable"
            ) from error

    def append(
        self,
        predicted: int,
        observed_at: datetime,
        *,
        duration_ms: float | None = None,
        inputs: dict[str, object] | None = None,
        channel: Literal["api", "streamlit"] = "api",
        positive_probability: float | None = None,
        decision_threshold: float | None = None,
        transaction_id: str | None = None,
    ) -> PredictionEvent:
        from API.telemetry.business import feature_json

        input_ref = (
            "sha256:" + sha256(feature_json(inputs).encode()).hexdigest()
            if inputs is not None
            else None
        )
        event = PredictionEvent(
            **self.scope.model_dump(),
            model_version=self.model_version,
            observation_id=str(uuid4()),
            source_ref="sha256:" + self.model_sha256,
            observed_at=observed_at,
            recorded_at=self._clock(),
            predicted=predicted,
            positive_probability=positive_probability,
            decision_threshold=decision_threshold,
            input_ref=input_ref,
        )
        raw = canonical_event(event)
        if len(raw) > MAX_RECORD_BYTES:
            raise PredictionLogError("Prediction record exceeds the byte limit")
        with self._connect() as connection:
            verify_database(connection)
            connection.execute(
                """INSERT INTO predictions
                (observation_id,tenant_id,project_id,model_id,model_version,observed_at,payload,digest)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    event.observation_id,
                    event.tenant_id,
                    event.project_id,
                    event.model_id,
                    event.model_version,
                    utc_text(event.observed_at),
                    raw,
                    sha256(raw).hexdigest(),
                ),
            )
            from API.telemetry.business import insert_transaction

            insert_transaction(connection, event, inputs or {}, channel)
            if transaction_id is not None:
                from API.telemetry.business_events import bind_transaction

                bind_transaction(connection, self.scope, event, transaction_id)
            if duration_ms is not None:
                from API.telemetry.operations import insert_operation, operation

                insert_operation(
                    connection,
                    operation(
                        self,
                        inputs or {},
                        duration_ms,
                        channel=channel,
                        observation_id=event.observation_id,
                        observed_at=event.observed_at,
                    ),
                )
        return event
