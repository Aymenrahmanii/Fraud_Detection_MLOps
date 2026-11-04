"""Read-only, scoped SQLite/PostgreSQL outcome outbox ingestion with durable cursors."""

import importlib
import json
import re
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent6_mlops_lifecycle.monitoring.business_models import (
    OutcomeFeedStatus,
    SystemOutcomeEvent,
)
from agent6_mlops_lifecycle.monitoring.models import OutcomeSubmission
from agent6_mlops_lifecycle.monitoring.settings import ROOT, MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    OutcomeNotFoundError,
)
from agent6_mlops_lifecycle.monitoring.state import MonitorState

COLUMNS = (
    "sequence_id",
    "event_id",
    "observation_id",
    "model_version",
    "actual",
    "evidence_ref",
    "occurred_at",
    "tenant_id",
    "project_id",
    "model_id",
)
TRANSACTION_COLUMNS = (
    "sequence_id",
    "event_id",
    "transaction_id",
    "source_id",
    "status",
    "verification_status",
    "evidence_ref",
    "occurred_at",
    "tenant_id",
    "project_id",
    "model_id",
)


def identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Business table and column identifiers must be simple SQL names")
    return '"' + name + '"'


def _rows(settings: MonitoringSettings, after: int) -> list[tuple[Any, ...]]:
    url = settings.business_database_url.get_secret_value()
    table = ".".join(identifier(part) for part in settings.business_outcome_table.split("."))
    selected_columns = (
        TRANSACTION_COLUMNS if settings.business_feed_mode == "transaction" else COLUMNS
    )
    columns = {
        name: identifier(settings.business_columns.get(name, name)) for name in selected_columns
    }
    selection = ",".join(columns[name] for name in selected_columns)
    postgres = url.startswith(("postgresql://", "postgres://"))
    placeholder = "%s" if postgres else "?"
    # Identifiers are validated independently; every data value is bound as a parameter.
    query = (
        f"SELECT {selection} FROM {table} WHERE {columns['sequence_id']}>{placeholder} "
        f"AND {columns['tenant_id']}={placeholder} AND {columns['project_id']}={placeholder} "
        f"AND {columns['model_id']}={placeholder} "
        f"ORDER BY {columns['sequence_id']} LIMIT {placeholder}"
    )
    parameters = (
        after,
        settings.tenant_id,
        settings.project_id,
        settings.model_id,
        settings.business_batch_size,
    )
    if postgres:
        driver = importlib.import_module("psycopg")
        with driver.connect(
            url, connect_timeout=5, options="-c statement_timeout=5000"
        ) as connection:
            connection.read_only = True
            return list(connection.execute(query, parameters).fetchall())
    if not url.startswith("sqlite:///"):
        raise ValueError("Only SQLite and PostgreSQL outcome databases are supported")
    path = Path(url[len("sqlite:///") :])
    path = (path if path.is_absolute() else ROOT / path).resolve()
    if path in (settings.state_database, settings.evidence_database):
        raise ValueError("The business outcome database must be separate from monitor stores")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.execute("PRAGMA query_only=ON")
        return connection.execute(query, parameters).fetchall()


def poll_outcomes(
    settings: MonitoringSettings, source: DirectObservationSource, state: MonitorState
) -> OutcomeFeedStatus:
    url = settings.business_database_url.get_secret_value()
    if not url:
        return OutcomeFeedStatus(state="NOT_CONFIGURED")
    binding = sha256(
        json.dumps(
            [
                url,
                settings.business_outcome_table,
                settings.business_columns,
                settings.business_feed_mode,
                settings.business_source_ids,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cursor = state.outcome_cursor(binding)
    processed = 0
    try:
        if settings.business_feed_mode == "transaction":
            # Flush already durable local resolutions even during an upstream outage.
            local = settings.model_copy(
                update={
                    "business_feed_mode": "observation",
                    "business_database_url": type(settings.business_database_url)(
                        "sqlite:///" + str(settings.source_database)
                    ),
                    "business_outcome_table": "agent6_business_outcomes_v1",
                    "business_columns": {},
                }
            )
            local_status = poll_outcomes(local, source, state)
            if local_status.state == "ERROR":
                return local_status
        rows = _rows(settings, cursor)
        selected_columns = (
            TRANSACTION_COLUMNS if settings.business_feed_mode == "transaction" else COLUMNS
        )
        for row in rows:
            if len(row) != len(selected_columns):
                raise ValueError("Unexpected business outbox shape")
            data = dict(zip(selected_columns, row, strict=True))
            sequence = data.pop("sequence_id")
            if type(sequence) is not int or sequence <= cursor:
                raise ValueError("Business outbox sequence must increase monotonically")
            if tuple(data.pop(name) for name in ("tenant_id", "project_id", "model_id")) != (
                settings.tenant_id,
                settings.project_id,
                settings.model_id,
            ):
                raise ValueError("Business outcome scope is invalid")
            if settings.business_feed_mode == "transaction":
                from API.telemetry.business_events import BusinessEvent, receive_event
                from API.telemetry.contracts import Scope

                business_event = BusinessEvent.model_validate(data)
                with source._connection(write=True) as connection:
                    receive_event(
                        connection,
                        Scope(**settings.scope().model_dump()),
                        business_event,
                        settings.business_source_ids,
                    )
                # The durable inbox retains unmatched transactions without blocking later events.
                state.save_outcome_cursor(binding, sequence)
                cursor, processed = sequence, processed + 1
                continue
            event = SystemOutcomeEvent.model_validate(data)
            prediction = source.get_prediction(str(event.observation_id))
            if prediction.model_version != event.model_version:
                raise ValueError("Business event model version does not match its prediction")
            source.submit_outcome(
                str(event.observation_id),
                OutcomeSubmission(
                    actual=event.actual,
                    note="Trusted business outcome: " + event.evidence_ref,
                    verified=True,
                    kind="SYSTEM",
                    event_id=str(event.event_id),
                    evidence_ref=event.evidence_ref,
                    occurred_at=event.occurred_at,
                ),
                "service:" + settings.outcome_source_id,
            )
            # A crash after storage but before this checkpoint safely replays the same event.
            state.save_outcome_cursor(binding, sequence)
            cursor, processed = sequence, processed + 1
        if settings.business_feed_mode == "transaction":
            # Always read the locally resolved outbox too, even if the external feed is idle.
            local = settings.model_copy(
                update={
                    "business_feed_mode": "observation",
                    "business_database_url": type(settings.business_database_url)(
                        "sqlite:///" + str(settings.source_database)
                    ),
                    "business_outcome_table": "agent6_business_outcomes_v1",
                    "business_columns": {},
                }
            )
            local_status = poll_outcomes(local, source, state)
            if local_status.state == "ERROR":
                return local_status
        return OutcomeFeedStatus(
            state="CONNECTED", last_sequence=cursor, processed_this_poll=processed
        )
    except OutcomeNotFoundError:
        return OutcomeFeedStatus(
            state="WAITING_FOR_PREDICTION",
            last_sequence=cursor,
            processed_this_poll=processed,
            error="Waiting for a matching prediction; event retained",
        )
    except Exception:
        # Do not expose database URLs, credentials or driver exception messages.
        return OutcomeFeedStatus(
            state="ERROR",
            last_sequence=cursor,
            processed_this_poll=processed,
            error="Outcome database unavailable or event invalid; checkpoint retained",
        )
