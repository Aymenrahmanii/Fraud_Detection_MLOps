"""Deterministic release-gate policy.

Scan results remain authoritative: this function contains the entire
BLOCK / ALLOW_WITH_WARNING / ALLOW decision. No LLM involvement here —
see the plan's Phase 3 notes for where natural-language rationale gets
layered on top, without ever touching this logic.
"""

from agent3_security_quality.core.schemas import (
    Decision,
    QualityResults,
    SecurityResults,
    TestResults,
)

MIN_COVERAGE_PCT = 70.0


def decide(
    tests: TestResults,
    quality: QualityResults,
    security: SecurityResults,
) -> tuple[Decision, list[str], list[str]]:
    """Return (decision, blocking_reasons, warnings)."""
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    if tests.failed > 0:
        blocking_reasons.append(f"{tests.failed} test(s) failing")

    if quality.gate == "FAILED":
        blocking_reasons.append("quality gate FAILED")

    if security.critical > 0:
        blocking_reasons.append(f"{security.critical} critical vulnerability(ies)")

    if security.secrets_found > 0:
        blocking_reasons.append(f"{security.secrets_found} secret(s) found in scan")

    if blocking_reasons:
        return "BLOCK", blocking_reasons, warnings

    if security.high > 0 and not security.policy_exception_required:
        warnings.append(f"{security.high} high-severity vulnerability(ies), no policy exception required")

    if tests.coverage_pct < MIN_COVERAGE_PCT:
        warnings.append(
            f"coverage {tests.coverage_pct:.1f}% below target {MIN_COVERAGE_PCT:.1f}%"
        )

    if tests.flaky_detected > 0:
        warnings.append(f"{tests.flaky_detected} flaky test(s) detected")

    decision: Decision = "ALLOW_WITH_WARNING" if warnings else "ALLOW"
    return decision, blocking_reasons, warnings
