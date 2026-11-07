"""Replaceable source boundary for bounded observation snapshots."""

from dataclasses import dataclass
from typing import Protocol

from agent6_mlops_lifecycle.contracts.mlops_ingestion import OutcomeRecord, PredictionRecord


@dataclass(frozen=True)
class ObservationSnapshot:
    predictions: tuple[PredictionRecord, ...]
    outcomes: tuple[OutcomeRecord, ...]
    prediction_source_ref: str
    outcome_source_ref: str


class ObservationSource(Protocol):
    def read_snapshot(self) -> ObservationSnapshot: ...
