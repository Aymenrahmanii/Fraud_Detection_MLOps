"""Execute explicit metric gates after bounded evidence eligibility checks."""

from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite

from agent6_mlops_lifecycle.contracts.mlops_metric_policy import MetricGateResult
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    BinaryEvaluationSample,
    EvaluationBatch,
    ProjectEvaluationReport,
    ProjectManifest,
    TaskType,
)
from agent6_mlops_lifecycle.evaluation.metric_registry import MetricRegistry


class MetricPolicyEvaluator:
    evaluator_id = "metric-policy-v2"

    def __init__(
        self, task_type: TaskType, registry: MetricRegistry, clock: Callable[[], datetime]
    ) -> None:
        self.task_type = task_type
        self._registry = registry
        self._clock = clock

    async def evaluate(
        self, manifest: ProjectManifest, batch: EvaluationBatch
    ) -> ProjectEvaluationReport:
        policy = manifest.metric_policy
        if policy is None:
            raise ValueError("Metric evaluator requires an explicit v2 policy")
        self._registry.validate_policy(manifest.task_type, policy)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Evaluation clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        context = batch.evidence_context
        coverage = len(batch.samples) / context.available_prediction_count if context else None
        age = max(0.0, (now - context.window_end).total_seconds()) if context else None
        reasons: list[str] = []
        if len(batch.samples) < manifest.min_samples:
            reasons.append("Fewer labeled samples than the project policy requires")
        if manifest.task_type == "binary_classification":
            positives = sum(
                row.expected for row in batch.samples if isinstance(row, BinaryEvaluationSample)
            )
            if positives < policy.min_positive_samples:
                reasons.append("Too few positive reference labels")
            if len(batch.samples) - positives < policy.min_negative_samples:
                reasons.append("Too few negative reference labels")
        if context is not None and context.as_of > now:
            reasons.append("Evidence cutoff is later than the evaluation clock")
        if policy.min_outcome_coverage is not None:
            if coverage is None:
                reasons.append("Outcome coverage metadata is missing")
            elif coverage < policy.min_outcome_coverage:
                reasons.append("Outcome coverage is below the project requirement")
        if policy.max_evidence_age_seconds is not None:
            if age is None:
                reasons.append("Evidence freshness metadata is missing")
            elif age > policy.max_evidence_age_seconds:
                reasons.append("Evidence exceeds the maximum allowed age")
        common = dict(
            project_id=manifest.project_id,
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            task_type=manifest.task_type,
            evaluator_id=manifest.evaluator_id,
            policy_version=manifest.policy_version,
            dataset_ref=batch.dataset_ref,
            evidence_ref=batch.evidence_ref,
            sample_count=len(batch.samples),
            requires_human_review=True,
            metric_policy_version="2",
            evaluated_at=now,
            outcome_coverage=coverage,
            evidence_age_seconds=age,
        )
        if reasons:
            return ProjectEvaluationReport.model_validate(
                {
                    **common,
                    "decision": "INSUFFICIENT_EVIDENCE",
                    "metrics": {},
                    "rationale_summary": "; ".join(reasons),
                }
            )
        gates = []
        metrics = {}
        for gate in policy.gates:
            result = self._registry.compute(gate.metric_id, batch)
            value = result.value
            if value is None:
                gates.append(
                    MetricGateResult(
                        gate=gate,
                        value=None,
                        status="UNAVAILABLE",
                        unavailable_reason=result.unavailable_reason,
                    )
                )
                continue
            drop = (
                (
                    gate.baseline.value - value
                    if gate.operator == "gte"
                    else value - gate.baseline.value
                )
                if gate.baseline
                else None
            )
            if drop is not None and not isfinite(drop):
                gates.append(
                    MetricGateResult(
                        gate=gate,
                        value=None,
                        status="UNAVAILABLE",
                        unavailable_reason="Baseline comparison overflow",
                    )
                )
                continue
            metrics[gate.metric_id] = value
            passed = (
                value >= gate.threshold if gate.operator == "gte" else value <= gate.threshold
            ) and (
                gate.baseline is None
                or (drop is not None and drop <= gate.baseline.max_degradation)
            )
            gates.append(
                MetricGateResult(
                    gate=gate, value=value, status="PASS" if passed else "FAIL", degradation=drop
                )
            )
        if any(gate.status == "UNAVAILABLE" for gate in gates):
            decision = "INSUFFICIENT_EVIDENCE"
            rationale = "A required metric is unavailable; inspect individual gate results"
        elif any(gate.status == "FAIL" for gate in gates):
            decision = "FAIL"
            rationale = "One or more required metric gates failed"
        else:
            decision = "PASS"
            rationale = "All configured metric gates passed; promotion still requires human review"
        return ProjectEvaluationReport.model_validate(
            {
                **common,
                "decision": decision,
                "metrics": metrics,
                "gate_results": tuple(gates),
                "rationale_summary": rationale,
            }
        )
