"""Agent 3 (Security & Quality) output contract.

This mirrors the RELEASE_ASSESSMENT payload defined in the architecture
spec (topic: quality.release.assessed). Keep this schema stable — Agent 4
and the Orchestrator are written against this shape.
"""

from typing import Literal

from pydantic import BaseModel, Field

Decision = Literal["BLOCK", "ALLOW_WITH_WARNING", "ALLOW"]


class TestResults(BaseModel):
    total: int
    passed: int
    failed: int
    skipped: int
    coverage_pct: float
    flaky_detected: int = 0


class QualityResults(BaseModel):
    gate: Literal["PASSED", "FAILED"]
    new_critical_issues: int = 0
    duplication_pct: float = 0.0


class SecurityResults(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    secrets_found: int = 0
    highest_cvss: float = 0.0
    policy_exception_required: bool = False


class ArtifactRef(BaseModel):
    image: str
    digest: str = ""


class ReleaseAssessment(BaseModel):
    schema_version: str = "1.0"
    assessment_id: str
    commit: str
    artifact: ArtifactRef
    tests: TestResults
    quality: QualityResults
    security: SecurityResults
    decision: Decision
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    policy_version: str = "release-policy-1.0"
