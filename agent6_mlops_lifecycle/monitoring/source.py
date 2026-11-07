"""Direct, scoped SQLite observation adapter and immutable attributed feedback."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_ingestion import OutcomeRecord, PredictionRecord
from agent6_mlops_lifecycle.interfaces.observations import ObservationSnapshot
from agent6_mlops_lifecycle.monitoring.models import (
    MonitorScope,
    OutcomeAudit,
    OutcomeSubmission,
)

SOURCE_APPLICATION_ID = 0x41365052
MAX_RECORDS = 20_000
MAX_PREDICTION_BYTES = 4096
MAX_OUTCOME_BYTES = 8192


class SourceUnavailableError(RuntimeError):
    pass


class OutcomeNotFoundError(ValueError):
    pass


class OutcomeConflictError(ValueError):
    pass


def canonical(value: Contract) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware time is required")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class StoredOutcome(Contract):
    event: OutcomeRecord
    audit: OutcomeAudit

    @model_validator(mode="after")
    def validate_audit(self) -> "StoredOutcome":
        if (
            self.event.observation_id != self.audit.observation_id
            or self.event.actual != self.audit.submission.actual
            or self.event.confirmed_at != self.audit.confirmed_at
            or self.event.source_ref != "sha256:" + sha256(canonical(self.audit)).hexdigest()
        ):
            raise ValueError("Outcome event differs from its immutable audit")
        return self


@dataclass(frozen=True)
class SourceFrame:
    model_version: str
    snapshot: ObservationSnapshot
    kinds: dict[str, str]


class DirectObservationSource:
    def __init__(
        self, database: Path, scope: MonitorScope, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.database = database.resolve()
        self.scope = scope
        self._clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.database.is_file():
            raise SourceUnavailableError("Waiting for the fraud API's first prediction")
        try:
            with closing(
                sqlite3.connect(
                    self.database.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                    uri=True,
                    timeout=5,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                with connection:
                    if (
                        connection.execute("PRAGMA application_id").fetchone()[0]
                        != SOURCE_APPLICATION_ID
                        or connection.execute("PRAGMA user_version").fetchone()[0] != 1
                    ):
                        raise SourceUnavailableError("Source is not a supported prediction log")
                    yield connection
        except sqlite3.Error as error:
            raise SourceUnavailableError("The prediction database is unavailable") from error

    def initialize_feedback(self) -> None:
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS agent6_confirmed_outcomes_v1 (
                observation_id TEXT PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS agent6_outcome_event_ids_v1 (
                event_key TEXT PRIMARY KEY, observation_id TEXT NOT NULL, digest TEXT NOT NULL
            )""")
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(agent6_confirmed_outcomes_v1)")
            }
            if columns != {"observation_id", "payload", "digest"}:
                raise SourceUnavailableError("Unexpected outcome table schema")

    def _prediction(self, row: sqlite3.Row) -> PredictionRecord:
        if row["payload"] is None:
            raise SourceUnavailableError("Prediction exceeds the allowed byte limit")
        raw = bytes(row["payload"])
        if len(raw) > MAX_PREDICTION_BYTES or sha256(raw).hexdigest() != row["digest"]:
            raise SourceUnavailableError("Prediction integrity check failed")
        try:
            event = PredictionRecord.model_validate_json(raw)
            if event.task_type != "binary_classification":
                raise ValueError("Serving source requires binary predictions")
            for name in ("observation_id", "tenant_id", "project_id", "model_id", "model_version"):
                if getattr(event, name) != row[name]:
                    raise ValueError("Prediction differs from its indexed identity")
            if utc(event.observed_at) != row["observed_at"]:
                raise ValueError("Prediction differs from its indexed observation time")
            if (event.tenant_id, event.project_id, event.model_id) != (
                self.scope.tenant_id,
                self.scope.project_id,
                self.scope.model_id,
            ):
                raise ValueError("Prediction differs from configured scope")
            return event
        except ValueError as error:
            raise SourceUnavailableError("Stored prediction schema or scope is invalid") from error

    def _outcome(self, row: sqlite3.Row, prediction: PredictionRecord) -> StoredOutcome:
        if row["payload"] is None:
            raise SourceUnavailableError("Outcome exceeds the byte limit")
        raw = bytes(row["payload"])
        if len(raw) > MAX_OUTCOME_BYTES or sha256(raw).hexdigest() != row["digest"]:
            raise SourceUnavailableError("Outcome integrity check failed")
        try:
            result = StoredOutcome.model_validate_json(raw)
            if result.event.observation_id != row["observation_id"]:
                raise ValueError("Outcome index differs from its record")
            for name in (
                "observation_id",
                "tenant_id",
                "project_id",
                "model_id",
                "model_version",
                "task_type",
            ):
                if getattr(result.event, name) != getattr(prediction, name):
                    raise ValueError("Outcome differs from its source prediction")
            if result.event.confirmed_at < prediction.observed_at:
                raise ValueError("Outcome precedes prediction")
            return result
        except ValueError as error:
            raise SourceUnavailableError("Stored outcome schema or scope is invalid") from error

    def read_frames(
        self, start: datetime, end: datetime, cutoff: datetime
    ) -> tuple[SourceFrame, ...]:
        if not start < end <= cutoff:
            raise ValueError("Source window requires start < end <= cutoff")
        with self._connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT observation_id,tenant_id,project_id,model_id,model_version,observed_at,"
                "CASE WHEN length(payload)<=? THEN payload ELSE NULL END AS payload,digest "
                "FROM predictions WHERE tenant_id=? AND project_id=? AND model_id=? "
                "AND observed_at>=? AND observed_at<? ORDER BY observed_at,observation_id LIMIT ?",
                (
                    MAX_PREDICTION_BYTES,
                    self.scope.tenant_id,
                    self.scope.project_id,
                    self.scope.model_id,
                    utc(start),
                    utc(end),
                    MAX_RECORDS + 1,
                ),
            ).fetchall()
            if len(rows) > MAX_RECORDS:
                raise SourceUnavailableError("Window exceeds 20,000 predictions; narrow the window")
            predictions = [self._prediction(row) for row in rows]
            predictions = [row for row in predictions if row.recorded_at <= cutoff]
            has_outcomes = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='agent6_confirmed_outcomes_v1'"
            ).fetchone()
            outcomes = {}
            if has_outcomes:
                indexed = {event.observation_id: event for event in predictions}
                rows = connection.execute(
                    "SELECT o.observation_id,CASE WHEN length(o.payload)<=? THEN "
                    "o.payload ELSE NULL END AS payload,o.digest FROM "
                    "agent6_confirmed_outcomes_v1 o JOIN predictions p "
                    "ON p.observation_id=o.observation_id WHERE p.tenant_id=? "
                    "AND p.project_id=? AND p.model_id=? AND p.observed_at>=? "
                    "AND p.observed_at<? LIMIT ?",
                    (
                        MAX_OUTCOME_BYTES,
                        self.scope.tenant_id,
                        self.scope.project_id,
                        self.scope.model_id,
                        utc(start),
                        utc(end),
                        MAX_RECORDS + 1,
                    ),
                ).fetchall()
                for row in rows:
                    prediction = indexed.get(row["observation_id"])
                    if prediction is not None:
                        result = self._outcome(row, prediction)
                        if result.event.confirmed_at <= cutoff:
                            outcomes[prediction.observation_id] = result
        versions = sorted({event.model_version for event in predictions})
        if len(versions) > 8:
            raise SourceUnavailableError("Window exceeds eight model versions; narrow its scope")
        frames = []
        for version in versions:
            selected = tuple(row for row in predictions if row.model_version == version)
            confirmed = tuple(
                outcomes[row.observation_id] for row in selected if row.observation_id in outcomes
            )
            outcome_events = tuple(row.event for row in confirmed)
            prediction_bytes = b"".join(canonical(row) + b"\n" for row in selected)
            outcome_bytes = b"".join(canonical(row) + b"\n" for row in outcome_events)
            frames.append(
                SourceFrame(
                    version,
                    ObservationSnapshot(
                        selected,
                        outcome_events,
                        "sha256:" + sha256(prediction_bytes).hexdigest(),
                        "sha256:" + sha256(outcome_bytes).hexdigest(),
                    ),
                    {row.event.observation_id: row.audit.submission.kind for row in confirmed},
                )
            )
        return tuple(frames)

    def first_predictions(
        self, version: str, count: int, cutoff: datetime
    ) -> tuple[PredictionRecord, ...]:
        if not 1 <= count <= MAX_RECORDS:
            raise ValueError("Reference count exceeds the bounded source limit")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT observation_id,tenant_id,project_id,model_id,model_version,observed_at,"
                "CASE WHEN length(payload)<=? THEN payload ELSE NULL END AS payload,digest "
                "FROM predictions WHERE tenant_id=? AND project_id=? AND model_id=? "
                "AND model_version=? AND observed_at<? ORDER BY observed_at,observation_id LIMIT ?",
                (
                    MAX_PREDICTION_BYTES,
                    self.scope.tenant_id,
                    self.scope.project_id,
                    self.scope.model_id,
                    version,
                    utc(cutoff),
                    count,
                ),
            ).fetchall()
        events = tuple(self._prediction(row) for row in rows)
        return tuple(event for event in events if event.recorded_at <= cutoff)

    def get_prediction(self, observation_id: str) -> PredictionRecord:
        with self._connection() as connection:
            row = connection.execute(
                (
                    "SELECT "
                    "observation_id,tenant_id,project_id,model_id,model_version,observed_at,CASE"
                    " WHEN length(payload)<=? THEN payload ELSE NULL END AS "
                    "payload,digest FROM predictions WHERE observation_id=? AND "
                    "tenant_id=? AND project_id=? AND model_id=?"
                ),
                (
                    MAX_PREDICTION_BYTES,
                    observation_id,
                    self.scope.tenant_id,
                    self.scope.project_id,
                    self.scope.model_id,
                ),
            ).fetchone()
        if row is None:
            raise OutcomeNotFoundError("Prediction does not exist in this scope")
        return self._prediction(row)

    def submit_outcome(
        self, observation_id: str, submission: OutcomeSubmission, actor: str
    ) -> StoredOutcome:
        submission = OutcomeSubmission.model_validate_json(submission.model_dump_json())
        self.initialize_feedback()
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                (
                    "SELECT "
                    "observation_id,tenant_id,project_id,model_id,model_version,observed_at,CASE"
                    " WHEN length(payload)<=? THEN payload ELSE NULL END AS "
                    "payload,digest FROM predictions WHERE observation_id=? AND "
                    "tenant_id=? AND project_id=? AND model_id=?"
                ),
                (
                    MAX_PREDICTION_BYTES,
                    observation_id,
                    self.scope.tenant_id,
                    self.scope.project_id,
                    self.scope.model_id,
                ),
            ).fetchone()
            if row is None:
                raise OutcomeNotFoundError("Prediction does not exist in this scope")
            prediction = self._prediction(row)
            event_key = None
            if submission.kind == "SYSTEM":
                now = self._clock()
                if (
                    submission.occurred_at is None
                    or not prediction.observed_at <= submission.occurred_at <= now
                ):
                    raise ValueError(
                        "Business outcome time must follow prediction and not be future"
                    )
                event_key = sha256((actor + ":" + str(submission.event_id)).encode()).hexdigest()
                known = connection.execute(
                    "SELECT observation_id,digest FROM agent6_outcome_event_ids_v1 "
                    "WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                if known and (
                    known["observation_id"] != observation_id
                    or known["digest"] != sha256(canonical(submission)).hexdigest()
                ):
                    raise OutcomeConflictError(
                        "Business event ID was reused with different evidence"
                    )
            existing = connection.execute(
                "SELECT observation_id,CASE WHEN length(payload)<=? THEN payload ELSE NULL END "
                "AS payload,digest FROM agent6_confirmed_outcomes_v1 WHERE observation_id=?",
                (MAX_OUTCOME_BYTES, observation_id),
            ).fetchone()
            if existing is not None:
                original = self._outcome(existing, prediction)
                if original.audit.submission != submission or original.audit.reviewer_id != actor:
                    raise OutcomeConflictError(
                        "Confirmed outcomes are immutable; conflicting review rejected"
                    )
                return original
            now = self._clock()
            utc(now)
            if now < prediction.observed_at:
                raise ValueError("Confirmation clock precedes prediction")
            audit = OutcomeAudit(
                observation_id=observation_id,
                reviewer_id=actor,
                submission=submission,
                confirmed_at=now,
            )
            event = OutcomeRecord(
                tenant_id=prediction.tenant_id,
                project_id=prediction.project_id,
                model_id=prediction.model_id,
                model_version=prediction.model_version,
                task_type=prediction.task_type,
                observation_id=observation_id,
                source_ref="sha256:" + sha256(canonical(audit)).hexdigest(),
                confirmed_at=now,
                actual=submission.actual,
            )
            stored = StoredOutcome(event=event, audit=audit)
            raw = canonical(stored)
            if len(raw) > MAX_OUTCOME_BYTES:
                raise ValueError("Outcome audit exceeds the byte limit")
            connection.execute(
                "INSERT INTO agent6_confirmed_outcomes_v1 VALUES(?,?,?)",
                (observation_id, raw, sha256(raw).hexdigest()),
            )
            if event_key is not None:
                connection.execute(
                    "INSERT INTO agent6_outcome_event_ids_v1 VALUES(?,?,?)",
                    (event_key, observation_id, sha256(canonical(submission)).hexdigest()),
                )
            return stored
