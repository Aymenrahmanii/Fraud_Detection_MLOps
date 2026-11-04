"""Validate LLM-selected investigations and render factual explanations from evidence."""

from typing import Any

from agent6_mlops_lifecycle.assurance.models import (
    AdvisorExplanation,
    AdvisorFinding,
    AdvisoryPlan,
)

CHECKS = {
    "REVIEW_BENCHMARK_GATES": (
        "Investigate subgroup metric gates that failed on the historical benchmark."
    ),
    "VERIFY_TRAINING_INDEPENDENCE": (
        "Obtain artifact-linked training provenance and verify the test rows were unused."
    ),
    "COLLECT_VERIFIED_OUTCOMES": (
        "Connect independently confirmed business outcomes to assess live correctness."
    ),
    "BUILD_DRIFT_REFERENCE": (
        "Collect sufficient reference and later observations before interpreting drift."
    ),
    "CHECK_SERVICE_HEALTH": "Investigate the endpoint reported DOWN by the service probe.",
    "INSPECT_SERVING_HEALTH": (
        "Investigate the measured serving error, input-quality or latency violations."
    ),
    "REVIEW_DISTRIBUTION_SHIFT": (
        "Investigate the measured distribution shift without treating it as proof of accuracy loss."
    ),
    "CALIBRATE_POLICY": (
        "Calibrate evaluation thresholds against business costs and representative evidence."
    ),
}


def eligible_checks(evidence: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for identity, item in evidence.items():
        checks = []
        if item.get("kind") == "LABELED_BENCHMARK":
            if any(group.get("decision") == "FAIL" for group in item.get("groups", {}).values()):
                checks.append("REVIEW_BENCHMARK_GATES")
            if item.get("training_independence") == "UNVERIFIED":
                checks.append("VERIFY_TRAINING_INDEPENDENCE")
            checks.append("CALIBRATE_POLICY")
        elif item.get("kind") == "LIVE_MONITOR":
            if item.get("quality_decision") in ("UNKNOWN", "INSUFFICIENT_EVIDENCE"):
                checks.append("COLLECT_VERIFIED_OUTCOMES")
            if item.get("drift", {}).get("status") in ("NO_BASELINE", "INSUFFICIENT_EVIDENCE"):
                checks.append("BUILD_DRIFT_REFERENCE")
            if (
                item.get("drift", {}).get("status") == "DETECTED"
                or (item.get("feature_drift") or {}).get("status") == "DETECTED"
            ):
                checks.append("REVIEW_DISTRIBUTION_SHIFT")
            if (item.get("operational_health") or {}).get("status") == "DEGRADED":
                checks.append("INSPECT_SERVING_HEALTH")
            if not item.get("policy_is_calibrated", False):
                checks.append("CALIBRATE_POLICY")
        elif (
            item.get("kind") == "CURRENT_SERVICE_HEALTH"
            and "DOWN" in item.get("services", {}).values()
        ):
            checks.append("CHECK_SERVICE_HEALTH")
        for check in checks:
            result.setdefault(check, []).append(identity)
    return result


def render_plan(evidence: dict[str, Any], plan: AdvisoryPlan) -> AdvisorExplanation:
    allowed = eligible_checks(evidence)
    findings = []
    checks = []
    seen = set()
    for selection in plan.findings:
        if selection.check_id in seen or not set(selection.evidence_ids).issubset(
            allowed.get(selection.check_id, [])
        ):
            raise ValueError("Advisor selected an unsupported investigation or citation")
        seen.add(selection.check_id)
        facts = []
        for identity in selection.evidence_ids:
            item = evidence[identity]
            if item.get("kind") == "LABELED_BENCHMARK":
                groups = item.get("groups", {})
                failed = (
                    ", ".join(
                        name for name, group in groups.items() if group.get("decision") == "FAIL"
                    )
                    or "none"
                )
                overall = groups.get("ALL", {}).get("decision", "UNKNOWN")
                facts.append(
                    f"{item.get('artifact_name', 'Artifact')} "
                    f"({item.get('model_version', 'unidentified')}): ALL={overall}; "
                    f"failed groups={failed}; "
                    f"training independence={item.get('training_independence', 'UNKNOWN')}"
                )
            elif item.get("kind") == "LIVE_MONITOR":
                source = (
                    "Scripted fixture model"
                    if item.get("evidence_origin") == "SCRIPTED_VERIFICATION_FIXTURES"
                    else "Live model"
                )
                facts.append(
                    f"{source} {item.get('model_version', 'unidentified')}: "
                    f"quality={item.get('quality_decision', 'UNKNOWN')}; "
                    f"drift={item.get('drift', {}).get('status', 'UNKNOWN')}"
                )
            elif item.get("kind") == "CURRENT_SERVICE_HEALTH":
                facts.append(
                    "Endpoint probes: "
                    + ", ".join(
                        f"{name}={state}" for name, state in item.get("services", {}).items()
                    )
                )
        finding = CHECKS[selection.check_id] + " " + "; ".join(facts)
        findings.append(
            AdvisorFinding(
                check_id=selection.check_id,
                text=finding[:1200],
                evidence_ids=selection.evidence_ids,
            )
        )
        checks.append(CHECKS[selection.check_id])
    facts = []
    for item in evidence.values():
        if item.get("kind") == "LABELED_BENCHMARK":
            overall = item.get("groups", {}).get("ALL", {})
            facts.append(
                f"{item.get('artifact_name', 'Artifact')}: "
                f"ALL={overall.get('decision', 'UNKNOWN')} on "
                f"{overall.get('sample_count', 0)} historical examples"
            )
    fixture_evidence = any(
        item.get("evidence_origin") == "SCRIPTED_VERIFICATION_FIXTURES"
        for item in evidence.values()
    )
    summary = "; ".join(facts) or (
        "Isolated SCRIPTED fixture evidence is available for investigation"
        if fixture_evidence
        else "Live evidence is available for investigation"
    )
    summary += (
        ". Benchmark results do not establish live accuracy. "
        "Drift is unknown without sufficient reference evidence. "
        "Groq selected the investigation steps; factual statements "
        "are rendered from measured evidence."
    )
    return AdvisorExplanation(
        summary=summary[:2000],
        findings=tuple(findings),
        recommended_checks=tuple(checks),
        limitations=(
            "Scripted verification outcomes do not establish production fraud correctness."
            if fixture_evidence
            else "Live correctness requires independently verified production outcomes.",
            "Existing artifact training independence is unverified.",
            (
                "Small positive-label groups and artificial class mixtures "
                "limit benchmark interpretation."
            ),
            "Thresholds require calibration; endpoint health and drift do not establish accuracy.",
            (
                "LLM investigation selection is advisory and can omit useful checks; "
                "measured gates remain authoritative."
            ),
        ),
    )
