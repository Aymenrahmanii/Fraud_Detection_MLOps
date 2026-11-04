"""Trusted business events and database-connector status."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_types import Name


class SystemOutcomeEvent(Contract):
    event_id: UUID
    observation_id: UUID
    model_version: Name
    actual: Annotated[int, Field(strict=True, ge=0, le=1)]
    evidence_ref: Name
    occurred_at: AwareDatetime


class OutcomeFeedStatus(Contract):
    state: Literal["NOT_CONFIGURED", "CONNECTED", "WAITING_FOR_PREDICTION", "ERROR"]
    last_sequence: Annotated[int, Field(strict=True, ge=0)] = 0
    processed_this_poll: Annotated[int, Field(strict=True, ge=0)] = 0
    error: str | None = None
