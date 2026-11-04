"""Shared project-evaluation identifiers, task families and numeric types."""

from typing import Annotated, Literal

from pydantic import Field

Name = Annotated[str, Field(min_length=1, max_length=160)]
UnitScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type TaskType = Literal["binary_classification", "regression", "text_generation", "custom"]
