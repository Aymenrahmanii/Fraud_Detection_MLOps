"""Bounded inference telemetry: timing, errors and allowlisted feature buckets only."""

import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal, cast
from uuid import uuid4

from pydantic import AwareDatetime, Field

from API.telemetry.contracts import Scope, utc_text

if TYPE_CHECKING:
    import sqlite3

    from API.telemetry.store import SQLitePredictionLog

FEATURES = {
    "accountAgeDays": (0, 7, 30, 90, 365, 1825),
    "numItems": (1, 2, 5, 10),
    "localTime": (0, 6, 12, 18, 24),
    "paymentMethodAgeDays": (0, 7, 30, 90, 365, 1825),
    "paymentMethod": ("paypal", "creditcard", "debitcard", "storecredit"),
    "Category": ("shopping", "electronics", "food", "other"),
    "isWeekend": (0, 1),
}


class ServingEvent(Scope):
    schema_version: Literal["1"] = "1"
    event_id: Annotated[str, Field(min_length=1, max_length=160)]
    model_version: Annotated[str, Field(min_length=1, max_length=160)]
    observation_id: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    observed_at: AwareDatetime
    recorded_at: AwareDatetime
    channel: Literal["api", "streamlit"]
    duration_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    success: bool
    status_code: Annotated[int, Field(ge=100, le=599)]
    feature_bins: dict[str, str]


def bucket_features(inputs: dict[str, object]) -> dict[str, str]:
    result = {}
    for name, boundaries in FEATURES.items():
        value = inputs.get(name)
        if value is None:
            result[name] = "MISSING"
        elif name in ("paymentMethod", "Category"):
            result[name] = (
                value if isinstance(value, str) and value in boundaries else "INVALID"
            )
        elif (
            not isinstance(value, (int, float))
            or type(value) not in (int, float)
            or not math.isfinite(value)
            or (
                (name == "isWeekend" and value not in (0, 1))
                or (name in ("accountAgeDays", "paymentMethodAgeDays") and value < 0)
                or (name == "numItems" and (value < 1 or int(value) != value))
                or (name == "localTime" and not 0 <= value < 24)
            )
        ):
            result[name] = "INVALID"
        elif name == "isWeekend":
            result[name] = str(int(value))
        else:
            result[name] = "bin-" + str(
                sum(value >= boundary for boundary in cast(tuple[int, ...], boundaries))
            )
    return result


def insert_operation(connection: "sqlite3.Connection", event: ServingEvent) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS agent6_serving_events_v1 (
        event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
        model_id TEXT NOT NULL, model_version TEXT NOT NULL, observed_at TEXT NOT NULL,
        payload BLOB NOT NULL, digest TEXT NOT NULL
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS agent6_operation_window
        ON agent6_serving_events_v1(tenant_id,project_id,model_id,observed_at)""")
    raw = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(raw) > 4096:
        raise ValueError("Operational telemetry exceeds the byte limit")
    connection.execute(
        "INSERT INTO agent6_serving_events_v1 VALUES(?,?,?,?,?,?,?,?)",
        (
            event.event_id,
            event.tenant_id,
            event.project_id,
            event.model_id,
            event.model_version,
            utc_text(event.observed_at),
            raw,
            sha256(raw).hexdigest(),
        ),
    )


def operation(
    log: "SQLitePredictionLog",
    inputs: dict[str, object],
    duration_ms: float,
    *,
    channel: Literal["api", "streamlit"] = "api",
    status_code: int = 200,
    observation_id: str | None = None,
    observed_at: datetime | None = None,
) -> ServingEvent:
    return ServingEvent(
        **log.scope.model_dump(),
        event_id=str(uuid4()),
        model_version=log.model_version,
        observation_id=observation_id,
        observed_at=observed_at or datetime.now(UTC),
        recorded_at=log._clock(),
        channel=channel,
        duration_ms=duration_ms,
        success=status_code < 400,
        status_code=status_code,
        feature_bins=bucket_features(inputs),
    )


def record_failure(
    log: "SQLitePredictionLog",
    inputs: dict[str, object],
    duration_ms: float,
    *,
    channel: Literal["api", "streamlit"] = "api",
    status_code: int = 500,
) -> None:
    event = operation(
        log, inputs, duration_ms, channel=channel, status_code=status_code
    )
    with log._connect() as connection:
        insert_operation(connection, event)
