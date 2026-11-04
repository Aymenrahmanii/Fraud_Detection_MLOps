"""Scoped references to immutable Agent 6 evaluation evidence."""

import json
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_projects import Name, ProjectManifest


class StoredEvaluationReference(Contract):
    schema_version: Literal["1"] = "1"
    tenant_id: Name
    project_id: Name
    model_id: Name
    model_version: Name
    policy_version: Name
    policy_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    dataset_ref: Name
    window_start: AwareDatetime
    window_end: AwareDatetime
    evidence_ref: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def check_window(self) -> "StoredEvaluationReference":
        if self.window_start >= self.window_end:
            raise ValueError("Evidence window start must precede its end")
        return self


def project_policy_fingerprint(manifest: ProjectManifest) -> str:
    """SHA-256 of canonical manifest JSON, binding all settings rather than its name only."""
    payload = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(payload).hexdigest()
