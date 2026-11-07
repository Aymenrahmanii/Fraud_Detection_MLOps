from typing import Annotated, Literal

from pydantic import Field

from agent6_mlops_lifecycle.contracts.base import Contract


class ModelHealthAssessed(Contract):
    assessment_id: str
    model_name: str
    model_version: str
    drift_detected: bool
    drift_score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    decision: Literal["HEALTHY", "COLLECT_LABELS", "RETRAIN_AND_EVALUATE"]
    evidence_refs: tuple[str, ...]
    dataset_ref: str | None = None
