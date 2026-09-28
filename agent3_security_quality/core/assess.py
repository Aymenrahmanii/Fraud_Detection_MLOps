"""Orchestrates the five scanners into one ReleaseAssessment.

Design choice: if a scanner tool is missing, that is a WARNING in local/dev
runs (so you can still iterate without every tool installed), but a
BLOCKING condition in --strict mode (i.e. CI). A release gate that quietly
skips a missing scanner and reports "ALLOW" is a fail-open gate, which is
the wrong default for anything touching production.
"""

import subprocess
from datetime import UTC, datetime

from agent3_security_quality.core.errors import ScannerError
from agent3_security_quality.core.policy import decide, merge_security
from agent3_security_quality.core.rationale import generate_rationale
from agent3_security_quality.core.scanners import (
    container_scanner,
    dependency_scanner,
    quality_scanner,
    secrets_scanner,
    test_runner,
)
from agent3_security_quality.core.schemas import (
    ArtifactRef,
    QualityResults,
    ReleaseAssessment,
    SecurityResults,
    TestResults,
)

EMPTY_TESTS = TestResults(total=0, passed=0, failed=0, skipped=0, coverage_pct=0.0)
EMPTY_QUALITY = QualityResults(gate="PASSED", new_critical_issues=0, duplication_pct=0.0)
EMPTY_SECURITY = SecurityResults()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def run_assessment(
    *,
    test_targets: tuple[str, ...] = ("agent3_security_quality/tests", "API/tests"),
    cov_sources: tuple[str, ...] = ("agent3_security_quality", "API"),
    quality_targets: tuple[str, ...] = ("API", "agent3_security_quality"),
    requirements_path: str = "requirements.txt",
    image_ref: str = "",
    strict: bool = False,
    with_rationale: bool = False,
) -> ReleaseAssessment:
    warnings: list[str] = []
    unavailable: list[str] = []

    def _try(name, fn, fallback):
        try:
            return fn()
        except ScannerError as exc:
            unavailable.append(f"{name}: {exc}")
            return fallback

    tests = _try(
        "tests",
        lambda: test_runner.run(*test_targets, cov_sources=cov_sources),
        EMPTY_TESTS,
    )
    quality = _try("quality", lambda: quality_scanner.run(*quality_targets), EMPTY_QUALITY)
    dependency_findings = _try(
        "dependencies", lambda: dependency_scanner.run(requirements_path), EMPTY_SECURITY
    )
    secrets_findings = _try("secrets", lambda: secrets_scanner.run("."), EMPTY_SECURITY)
    container_findings = (
        _try("container", lambda: container_scanner.run(image_ref), EMPTY_SECURITY)
        if image_ref
        else EMPTY_SECURITY
    )

    security = merge_security([dependency_findings, secrets_findings, container_findings])

    decision, blocking_reasons, policy_warnings = decide(tests, quality, security)
    warnings.extend(policy_warnings)

    if unavailable:
        if strict:
            blocking_reasons.extend(f"scanner unavailable: {u}" for u in unavailable)
            decision = "BLOCK"
        else:
            warnings.extend(f"scanner skipped (dev mode): {u}" for u in unavailable)

    assessment = ReleaseAssessment(
        assessment_id=f"REL-{datetime.now(UTC):%Y%m%d%H%M%S}",
        commit=_git_commit(),
        artifact=ArtifactRef(image=image_ref),
        tests=tests,
        quality=quality,
        security=security,
        decision=decision,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
    )

    if with_rationale:
        # Runs strictly after the decision above is final; generate_rationale
        # never raises (see its docstring), so this can't turn a completed
        # assessment into a failure.
        assessment.rationale = generate_rationale(assessment)

    return assessment
