"""Versioned policies, metric parity, evidence gates and compatibility checks."""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent6_mlops_lifecycle.adapters.jsonl_observations import JSONLObservationSource
from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.mlops_ingestion import (
    ObservationIngestionRequest,
    OutcomeRecord,
    PredictionRecord,
)
from agent6_mlops_lifecycle.contracts.mlops_metric_policy import (
    MetricDefinition,
    MetricGate,
    MetricGateResult,
    MetricValue,
)
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    EvaluationBatch,
    ProjectEvaluationReport,
    ProjectManifest,
)
from agent6_mlops_lifecycle.contracts.mlops_storage import project_policy_fingerprint
from agent6_mlops_lifecycle.core.errors import IntegrationNotConfiguredError
from agent6_mlops_lifecycle.evaluation.builtin_metrics import default_metric_registry
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService
from agent6_mlops_lifecycle.evaluation.metric_registry import MetricRegistry
from agent6_mlops_lifecycle.ingestion.service import ObservationIngestionService

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 30, tzinfo=UTC)
END = START + timedelta(days=1)
NOW = END + timedelta(days=2)


def policy(task="binary_classification", gates=None, **overrides):
    if gates is None:
        gates = (
            [{"metric_id": "binary.recall.v1", "operator": "gte", "threshold": 0.8}]
            if task == "binary_classification"
            else [
                {"metric_id": "regression.mae.v1", "operator": "lte", "threshold": 0.3},
            ]
        )
    return ProjectManifest.model_validate(
        {
            "project_id": "metrics-" + task,
            "model_id": "model",
            "model_version": "v2",
            "task_type": task,
            "policy_version": "reviewed-policy-v2",
            "evaluator_id": "metric-policy-v2",
            "min_samples": 2,
            "metric_policy": {"schema_version": "2", "gates": gates, **overrides},
        }
    )


def batch(task="binary_classification", pairs=None, probabilities=None, context=True):
    if pairs is None:
        pairs = (
            [(1, 1), (1, 0), (0, 1), (0, 0)]
            if task == "binary_classification"
            else [(10, 10.2), (12, 11.8)]
        )
    samples = []
    for index, (expected, predicted) in enumerate(pairs):
        row = {
            "task_type": task,
            "observation_id": str(index),
            "observed_at": START + timedelta(hours=index),
            "expected": expected,
            "predicted": predicted,
        }
        if probabilities is not None:
            row["positive_probability"] = probabilities[index]
        samples.append(row)
    payload = {
        "project_id": "metrics-" + task,
        "model_id": "model",
        "model_version": "v2",
        "dataset_ref": "synthetic-window",
        "evidence_ref": "local:prepared",
        "samples": samples,
    }
    if context:
        payload["evidence_context"] = {
            "window_start": START,
            "window_end": END,
            "as_of": NOW,
            "available_prediction_count": len(samples) + 1,
        }
    return EvaluationBatch.model_validate(payload)


def evaluate(manifest, evidence, registry=None):
    service = ProjectEvaluationService(metric_registry=registry, clock=lambda: NOW)
    service.register_project(manifest)
    return asyncio.run(service.evaluate(evidence))


def test_multiple_gates_require_all_checks_to_pass():
    manifest = policy(
        gates=[
            {"metric_id": "binary.recall.v1", "operator": "gte", "threshold": 0.4},
            {"metric_id": "binary.precision.v1", "operator": "gte", "threshold": 0.8},
        ]
    )
    report = evaluate(manifest, batch())
    assert report.decision == "FAIL"
    assert [gate.status for gate in report.gate_results] == ["PASS", "FAIL"]
    assert report.metrics == {"binary.recall.v1": 0.5, "binary.precision.v1": 0.5}
    assert report.requires_human_review
    assert report.metric_policy_version == "2"
    assert report.evaluated_at == NOW
    assert report.outcome_coverage == 0.8


def test_regression_policy_can_require_error_and_r2_together():
    manifest = policy(
        "regression",
        gates=[
            {"metric_id": "regression.mae.v1", "operator": "lte", "threshold": 0.3},
            {"metric_id": "regression.r2.v1", "operator": "gte", "threshold": 0.9},
        ],
    )
    report = evaluate(manifest, batch("regression"))
    assert report.decision == "PASS"
    assert report.metrics["regression.mae.v1"] == pytest.approx(0.2)
    assert report.metrics["regression.r2.v1"] == pytest.approx(0.96)


@pytest.mark.parametrize(
    "metric",
    [
        "accuracy",
        "precision",
        "recall",
        "f1",
        "specificity",
        "balanced_accuracy",
        "brier",
        "log_loss",
        "roc_auc",
    ],
)
def test_binary_metrics_agree_with_independent_sklearn_functions(metric):
    from sklearn import metrics

    rng = random.Random(67)
    actual = [rng.randrange(2) for _ in range(20)]
    probabilities = [rng.random() for _ in actual]
    predicted = [int(p >= 0.4) for p in probabilities]
    evidence = batch(pairs=list(zip(actual, predicted, strict=True)), probabilities=probabilities)
    functions = {
        "accuracy": lambda: metrics.accuracy_score(actual, predicted),
        "precision": lambda: metrics.precision_score(actual, predicted),
        "recall": lambda: metrics.recall_score(actual, predicted),
        "f1": lambda: metrics.f1_score(actual, predicted),
        "specificity": lambda: metrics.recall_score(actual, predicted, pos_label=0),
        "balanced_accuracy": lambda: metrics.balanced_accuracy_score(actual, predicted),
        "brier": lambda: metrics.brier_score_loss(actual, probabilities),
        "log_loss": lambda: metrics.log_loss(actual, probabilities),
        "roc_auc": lambda: metrics.roc_auc_score(actual, probabilities),
    }
    result = default_metric_registry().compute("binary." + metric + ".v1", evidence)
    assert result.value == pytest.approx(float(functions[metric]()))


@pytest.mark.parametrize("metric", ["mae", "mse", "rmse", "r2"])
def test_regression_metrics_agree_with_independent_sklearn_functions(metric):
    from sklearn import metrics

    rng = random.Random(18)
    actual = [rng.uniform(-5, 5) for _ in range(12)]
    predicted = [value + rng.uniform(-1, 1) for value in actual]
    evidence = batch("regression", pairs=list(zip(actual, predicted, strict=True)))
    functions = {
        "mae": metrics.mean_absolute_error,
        "mse": metrics.mean_squared_error,
        "rmse": metrics.root_mean_squared_error,
        "r2": metrics.r2_score,
    }
    result = default_metric_registry().compute("regression." + metric + ".v1", evidence)
    assert result.value == pytest.approx(float(functions[metric](actual, predicted)))


@pytest.mark.parametrize(
    "probabilities,expected",
    [
        ([0.5, 0.5, 0.5, 0.5], 0.5),
        ([1.0, 1.0, 0.0, 0.0], 1.0),
        ([0.0, 0.0, 1.0, 1.0], 0.0),
    ],
)
def test_auc_handles_ties_and_extreme_probabilities(probabilities, expected):
    evidence = batch(pairs=[(1, 1), (1, 1), (0, 0), (0, 0)], probabilities=probabilities)
    assert default_metric_registry().compute("binary.roc_auc.v1", evidence).value == expected


def test_log_loss_epsilon_matches_sklearn_at_zero_and_one():
    from sklearn.metrics import log_loss

    evidence = batch(pairs=[(1, 0), (0, 1)], probabilities=[0.0, 1.0])
    assert default_metric_registry().compute("binary.log_loss.v1", evidence).value == pytest.approx(
        log_loss([1, 0], [0.0, 1.0])
    )


@pytest.mark.parametrize(
    "metric,pairs",
    [
        ("precision", [(1, 0), (0, 0)]),
        ("recall", [(0, 0), (0, 1)]),
        ("f1", [(0, 0), (0, 0)]),
        ("specificity", [(1, 1), (1, 0)]),
        ("balanced_accuracy", [(1, 1), (1, 1)]),
        ("roc_auc", [(1, 1), (1, 0)]),
    ],
)
def test_undefined_binary_metrics_never_become_fabricated_zero(metric, pairs):
    metric_id = "binary." + metric + ".v1"
    report = evaluate(
        policy(gates=[{"metric_id": metric_id, "operator": "gte", "threshold": 0.5}]),
        batch(pairs=pairs, probabilities=[0.5, 0.5]),
    )
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert report.metrics == {}
    assert report.gate_results[0].status == "UNAVAILABLE"
    assert report.gate_results[0].value is None


def test_missing_probabilities_are_unavailable_even_with_good_class_predictions():
    report = evaluate(
        policy(
            gates=[
                {"metric_id": "binary.accuracy.v1", "operator": "gte", "threshold": 0.9},
                {"metric_id": "binary.roc_auc.v1", "operator": "gte", "threshold": 0.8},
            ]
        ),
        batch(pairs=[(1, 1), (0, 0)]),
    )
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert report.metrics == {"binary.accuracy.v1": 1.0}
    assert report.gate_results[1].unavailable_reason == "Positive-class probabilities are missing"


def test_constant_regression_values_make_r2_unavailable():
    report = evaluate(
        policy(
            "regression",
            gates=[
                {"metric_id": "regression.r2.v1", "operator": "gte", "threshold": 0.8},
            ],
        ),
        batch("regression", pairs=[(5, 5), (5, 5)]),
    )
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert report.gate_results[0].value is None


@pytest.mark.parametrize(
    "metric,operator,threshold",
    [
        ("binary.unknown.v1", "gte", 0.5),
        ("regression.mae.v1", "lte", 0.5),
        ("binary.recall.v1", "lte", 0.5),
        ("binary.recall.v1", "gte", 2),
    ],
)
def test_unsupported_metrics_tasks_directions_and_domains_stop_registration(
    metric, operator, threshold
):
    manifest = policy(gates=[{"metric_id": metric, "operator": operator, "threshold": threshold}])
    service = ProjectEvaluationService()
    with pytest.raises((ValueError, IntegrationNotConfiguredError)):
        service.register_project(manifest)


@pytest.mark.parametrize(
    "changes",
    [
        {"metric_policy": {"schema_version": "3", "gates": []}},
        {"metric_policy": {"schema_version": "2", "gates": []}},
        {"min_f1": 0.5},
        {"allow_external_llm": True},
        {"evaluator_id": "binary-f1-v1"},
    ],
)
def test_policy_versions_and_legacy_settings_cannot_be_mixed(changes):
    raw = policy().model_dump(mode="json")
    raw.update(changes)
    with pytest.raises(ValidationError):
        ProjectManifest.model_validate(raw)


def test_duplicate_gates_and_coerced_thresholds_fail():
    raw = policy().model_dump(mode="json")
    gate = raw["metric_policy"]["gates"][0]
    raw["metric_policy"]["gates"].append(dict(gate))
    with pytest.raises(ValidationError, match="only once"):
        ProjectManifest.model_validate(raw)
    for value in (True, "0.8", float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            MetricGate.model_validate({**gate, "threshold": value})


@pytest.mark.parametrize(
    "requirements,with_context,expected_reason",
    [
        ({"min_outcome_coverage": 0.9}, True, "coverage is below"),
        ({"min_outcome_coverage": 0.5}, False, "coverage metadata is missing"),
        ({"max_evidence_age_seconds": 3600}, True, "maximum allowed age"),
        ({"max_evidence_age_seconds": 3600}, False, "freshness metadata is missing"),
        ({"min_positive_samples": 3}, True, "positive reference"),
        ({"min_negative_samples": 3}, True, "negative reference"),
    ],
)
def test_evidence_requirements_stop_metrics_before_execution(
    requirements, with_context, expected_reason
):
    registry = MetricRegistry()

    def never_compute(evidence):
        raise AssertionError("Evidence failed eligibility; metric must not run")

    registry.register(
        MetricDefinition(
            metric_id="binary.check.v1",
            task_types=("binary_classification",),
            direction="gte",
            description="Eligibility sentinel",
            units="ratio",
            minimum=0,
            maximum=1,
        ),
        never_compute,
    )
    report = evaluate(
        policy(
            gates=[
                {"metric_id": "binary.check.v1", "operator": "gte", "threshold": 0.5},
            ],
            **requirements,
        ),
        batch(context=with_context),
        registry,
    )
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert expected_reason in report.rationale_summary
    assert report.metrics == {} and report.gate_results == ()


def test_freshness_uses_window_end_and_future_cutoff_fails():
    evidence = batch()
    report = evaluate(policy(max_evidence_age_seconds=3600), evidence)
    assert report.evidence_age_seconds == 172800
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    future = evidence.model_copy(
        update={
            "evidence_context": evidence.evidence_context.model_copy(
                update={"as_of": NOW + timedelta(seconds=1)}
            )
        }
    )
    report = evaluate(policy(), future)
    assert "later than the evaluation clock" in report.rationale_summary


@pytest.mark.parametrize(
    "task,metric,operator,pairs,baseline,value,drop",
    [
        ("binary_classification", "binary.recall.v1", "gte", [(1, 1), (1, 0)], 0.9, 0.5, 0.4),
        ("regression", "regression.mae.v1", "lte", [(1, 1.4), (2, 2.4)], 0.1, 0.4, 0.3),
    ],
)
def test_baseline_degradation_uses_correct_direction(
    task, metric, operator, pairs, baseline, value, drop
):
    report = evaluate(
        policy(
            task,
            gates=[
                {
                    "metric_id": metric,
                    "operator": operator,
                    "threshold": 0.4 if operator == "gte" else 0.5,
                    "baseline": {
                        "value": baseline,
                        "evidence_ref": "sha256:" + "1" * 64,
                        "max_degradation": 0.1,
                    },
                }
            ],
        ),
        batch(task, pairs=pairs),
    )
    assert report.decision == "FAIL"
    result = report.gate_results[0]
    assert result.value == pytest.approx(value)
    assert result.degradation == pytest.approx(drop)


def test_gate_boundaries_are_inclusive_and_improvements_pass_baseline_checks():
    report = evaluate(
        policy(
            gates=[
                {
                    "metric_id": "binary.recall.v1",
                    "operator": "gte",
                    "threshold": 0.5,
                    "baseline": {
                        "value": 0.4,
                        "evidence_ref": "sha256:" + "2" * 64,
                        "max_degradation": 0.0,
                    },
                }
            ]
        ),
        batch(),
    )
    assert report.decision == "PASS"
    assert report.gate_results[0].degradation == pytest.approx(-0.1)


def test_metric_registration_and_plugin_results_are_bounded_and_immutable():
    registry = MetricRegistry()
    definition = MetricDefinition(
        metric_id="custom.success.v1",
        task_types=("custom",),
        direction="gte",
        description="Reviewed success rate",
        units="ratio",
        minimum=0,
        maximum=1,
    )
    registry.register(definition, lambda evidence: MetricValue(value=1))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition, lambda evidence: MetricValue(value=0))
    custom_policy = policy(
        "custom",
        gates=[
            {"metric_id": definition.metric_id, "operator": "gte", "threshold": 0.8},
        ],
    )
    custom_batch = EvaluationBatch.model_validate(
        {
            "project_id": custom_policy.project_id,
            "model_id": "model",
            "model_version": "v2",
            "dataset_ref": "custom-cohort",
            "evidence_ref": "local:custom",
            "samples": [
                {
                    "task_type": "custom",
                    "observation_id": str(i),
                    "observed_at": START,
                    "payload": {"checked": True},
                }
                for i in range(2)
            ],
        }
    )
    report = evaluate(custom_policy, custom_batch, registry)
    assert report.decision == "PASS"
    bad = MetricRegistry()
    bad.register(definition, lambda evidence: MetricValue(value=2))
    with pytest.raises(ValueError, match="outside the domain"):
        evaluate(custom_policy, custom_batch, bad)
    invalid = MetricRegistry()
    invalid.register(definition, lambda evidence: MetricValue.model_construct(value=float("nan")))
    with pytest.raises(ValidationError):
        evaluate(custom_policy, custom_batch, invalid)


def test_numeric_extremes_do_not_return_infinite_or_fabricated_metrics():
    evidence = batch("regression", pairs=[(0, 1e308), (0, -1e308)])
    registry = default_metric_registry()
    assert registry.compute("regression.rmse.v1", evidence).value == pytest.approx(1e308)
    assert registry.compute("regression.mae.v1", evidence).value == pytest.approx(1e308)
    assert registry.compute("regression.mse.v1", evidence).value is None
    assert registry.compute("regression.r2.v1", evidence).value is None


def test_legacy_manifest_and_batch_serialization_remain_byte_equivalent():
    golden = json.loads((ROOT / "tests/fixtures/agent6-legacy-serialization.json").read_text())
    for record in golden:
        manifest = ProjectManifest.model_validate(record["manifest"])
        evidence = EvaluationBatch.model_validate(record["batch"])
        assert manifest.model_dump(mode="json") == record["manifest"]
        assert evidence.model_dump(mode="json") == record["batch"]
        canonical = json.dumps(
            record["manifest"], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        assert project_policy_fingerprint(manifest) == sha256(canonical.encode()).hexdigest()


def test_v2_policy_probability_and_context_survive_sqlite_restart(tmp_path):
    manifest = policy(
        gates=[
            {"metric_id": "binary.brier.v1", "operator": "lte", "threshold": 0.5},
        ],
        min_outcome_coverage=0.8,
    )
    evidence = batch(probabilities=[0.8, 0.3, 0.7, 0.2])
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    service = ProjectEvaluationService(manifests=store, batches=store, clock=lambda: NOW)
    service.register_project(manifest)
    reference = service.store_batch(evidence, START, END)
    reopened = SQLiteProjectStore(store.path)
    after = ProjectEvaluationService(manifests=reopened, batches=reopened, clock=lambda: NOW)
    report = asyncio.run(after.evaluate_reference(reference))
    assert report.decision == "PASS"
    assert report.outcome_coverage == 0.8
    assert report.evidence_ref == reference.evidence_ref
    assert reopened.load_batch(reference).samples[0].positive_probability == 0.8


def test_batch_context_cannot_lie_about_counts_or_stored_window(tmp_path):
    evidence = batch()
    raw = evidence.model_dump()
    raw["evidence_context"]["available_prediction_count"] = 1
    with pytest.raises(ValidationError, match="exceeds available"):
        EvaluationBatch.model_validate(raw)
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    manifest = policy()
    store.register_manifest("local", manifest)
    with pytest.raises(ValidationError, match="context differs"):
        store.save_batch("local", manifest, evidence, START - timedelta(days=1), END)


def test_ingestion_preserves_probability_and_derives_coverage_metadata(tmp_path):
    manifest = policy(
        gates=[
            {"metric_id": "binary.brier.v1", "operator": "lte", "threshold": 0.1},
        ],
        min_outcome_coverage=0.6,
    )
    query = ObservationIngestionRequest(
        tenant_id="local",
        project_id=manifest.project_id,
        model_id="model",
        model_version="v2",
        dataset_ref="synthetic",
        window_start=START,
        window_end=END,
        as_of=NOW,
    )
    common = dict(
        tenant_id="local",
        project_id=manifest.project_id,
        model_id="model",
        model_version="v2",
        task_type="binary_classification",
        source_ref="synthetic:producer",
    )
    predictions = [
        PredictionRecord(
            **common,
            observation_id=str(i),
            observed_at=START,
            recorded_at=START,
            predicted=int(i != 1),
            positive_probability=0.1 if i == 1 else 0.9,
        )
        for i in range(3)
    ]
    outcomes = [
        OutcomeRecord(
            **common,
            observation_id=str(i),
            confirmed_at=END,
            actual=int(i != 1),
        )
        for i in range(2)
    ]
    p = tmp_path / "p.jsonl"
    o = tmp_path / "o.jsonl"
    p.write_text("".join(row.model_dump_json() + "\n" for row in predictions))
    o.write_text("".join(row.model_dump_json() + "\n" for row in outcomes))
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    evaluator = ProjectEvaluationService(manifests=store, batches=store, clock=lambda: NOW)
    evaluator.register_project(manifest)
    receipt = ObservationIngestionService(manifests=store, batches=store, tenant_id="local").ingest(
        query, JSONLObservationSource(p, o)
    )
    assert receipt.summary.outcome_coverage == pytest.approx(2 / 3)
    report = asyncio.run(evaluator.evaluate_reference(receipt.reference))
    assert report.decision == "PASS"
    assert report.outcome_coverage == pytest.approx(2 / 3)
    assert report.metrics["binary.brier.v1"] == pytest.approx(0.01)


def test_gate_results_and_report_cannot_claim_false_pass_or_disable_review():
    report = evaluate(policy(), batch())
    assert report.decision == "FAIL"
    raw = report.model_dump()
    raw["decision"] = "PASS"
    with pytest.raises(ValidationError, match="every metric gate"):
        ProjectEvaluationReport.model_validate(raw)
    raw = report.gate_results[0].model_dump()
    raw["status"] = "PASS"
    with pytest.raises(ValidationError, match="status differs"):
        MetricGateResult.model_validate(raw)
    raw = report.model_dump()
    raw["requires_human_review"] = False
    with pytest.raises(ValidationError, match="require review"):
        ProjectEvaluationReport.model_validate(raw)


def test_minimum_sample_policy_returns_versioned_insufficient_report():
    manifest = policy().model_copy(update={"min_samples": 10})
    report = evaluate(manifest, batch())
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert report.metric_policy_version == "2"
    assert report.evaluated_at == NOW
    assert report.gate_results == ()


def test_modified_policy_cannot_replace_registered_v2_policy(tmp_path):
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    service = ProjectEvaluationService(manifests=store, batches=store)
    service.register_project(policy())
    with pytest.raises(RuntimeError, match="silently change"):
        service.register_project(policy(min_outcome_coverage=0.9))


def test_malformed_metric_plugin_cannot_rewrite_configured_gates():

    service = ProjectEvaluationService(clock=lambda: NOW)
    service.register_project(policy())
    evaluator = service._evaluators[("binary_classification", "metric-policy-v2")]

    class ChangedGates:
        task_type = "binary_classification"
        evaluator_id = "metric-policy-v2"

        async def evaluate(self, manifest, evidence):
            report = await evaluator.evaluate(manifest, evidence)
            altered = MetricGate(metric_id="binary.recall.v1", operator="gte", threshold=0.6)
            result = report.gate_results[0].model_copy(update={"gate": altered})
            return report.model_copy(update={"gate_results": (result,)})

    # Simulate a faulty trusted integration; public registration disallows replacement.
    service._evaluators[("binary_classification", "metric-policy-v2")] = ChangedGates()
    with pytest.raises(ValueError, match="differ from the registered policy"):
        asyncio.run(service.evaluate(batch()))


def test_required_unavailable_metric_takes_precedence_but_failed_gate_remains_visible():
    report = evaluate(
        policy(
            gates=[
                {"metric_id": "binary.recall.v1", "operator": "gte", "threshold": 0.8},
                {"metric_id": "binary.roc_auc.v1", "operator": "gte", "threshold": 0.8},
            ]
        ),
        batch(),
    )
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert [row.status for row in report.gate_results] == ["FAIL", "UNAVAILABLE"]


def test_schema_supports_v2_and_blocks_mixed_legacy_thresholds():
    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "contracts/jsonschema/v1/ProjectManifest.schema.json").read_text())
    validator = Draft202012Validator(schema)
    raw = policy().model_dump(mode="json")
    validator.validate(raw)
    assert list(validator.iter_errors({**raw, "min_f1": 0.5}))
    assert list(validator.iter_errors({**raw, "metric_policy": None}))


def test_demo_and_capability_cli_are_reproducible_without_an_llm(tmp_path):
    import subprocess
    import sys

    from agent6_mlops_lifecycle.cli.metric_policy_demo import run

    report = asyncio.run(run(tmp_path))
    assert [row["decision"] for row in report["projects"]] == ["FAIL", "PASS"]
    assert report["projects"][0]["gates"][0]["status"] == "FAIL"
    result = subprocess.run(
        [sys.executable, "-m", "agent6_mlops_lifecycle.cli.metrics"],
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    definitions = json.loads(result.stdout)
    assert len(definitions) == 13
    assert len({row["metric_id"] for row in definitions}) == 13


@pytest.mark.parametrize("value", [True, -0.1, 1.1, "0.5", float("nan")])
def test_probability_inputs_are_finite_bounded_and_not_coerced(value):
    evidence = batch(probabilities=[0.8, 0.3, 0.7, 0.2]).model_dump()
    evidence["samples"][0]["positive_probability"] = value
    with pytest.raises(ValidationError):
        EvaluationBatch.model_validate(evidence)


def test_probability_metadata_is_rejected_for_regression_records():
    with pytest.raises(ValidationError, match="apply only to binary"):
        PredictionRecord(
            tenant_id="local",
            project_id="energy",
            model_id="forecaster",
            model_version="v1",
            task_type="regression",
            observation_id="1",
            source_ref="synthetic:source",
            observed_at=START,
            recorded_at=START,
            predicted=10,
            positive_probability=0.5,
        )


def test_failed_policy_is_successful_task_execution_and_preserves_gate_details():
    from agent6_mlops_lifecycle.contracts.enums import AgentId, Environment, ResultStatus
    from agent6_mlops_lifecycle.contracts.tasks import AgentTask, TaskContext
    from agent6_mlops_lifecycle.service import MLOpsLifecycleAgent

    service = ProjectEvaluationService(clock=lambda: NOW)
    service.register_project(policy())
    evidence = batch()
    agent = MLOpsLifecycleAgent(project_evaluation=service)
    task = AgentTask(
        task_id="metric-task",
        incident_id="metric-incident",
        agent_id=AgentId.MLOPS_LIFECYCLE,
        objective="Evaluate required metrics",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        task_context=TaskContext(
            service_id="metrics",
            namespace="local",
            environment=Environment.TEST,
            signals={"project_evaluation": evidence.model_dump(mode="json")},
            evidence_refs=(evidence.evidence_ref,),
        ),
    )
    result = asyncio.run(agent.handle(task))
    assert result.status == ResultStatus.SUCCEEDED
    assert result.payload["decision"] == "FAIL"
    assert result.payload["metric_policy_version"] == "2"
    assert result.payload["gate_results"][0]["status"] == "FAIL"


def test_custom_metrics_receive_independent_payloads_and_do_not_mutate_evidence():
    registry = MetricRegistry()

    def mutate(evidence):
        evidence.samples[0].payload["checked"] = False
        return MetricValue(value=1)

    def read(evidence):
        return MetricValue(value=int(evidence.samples[0].payload["checked"]))

    for name, function in (("mutate", mutate), ("read", read)):
        registry.register(
            MetricDefinition(
                metric_id="custom." + name + ".v1",
                task_types=("custom",),
                direction="gte",
                description="Plugin isolation fixture",
                units="ratio",
                minimum=0,
                maximum=1,
            ),
            function,
        )
    manifest = policy(
        "custom",
        gates=[
            {"metric_id": "custom.mutate.v1", "operator": "gte", "threshold": 1},
            {"metric_id": "custom.read.v1", "operator": "gte", "threshold": 1},
        ],
    )
    evidence = EvaluationBatch.model_validate(
        {
            "project_id": manifest.project_id,
            "model_id": "model",
            "model_version": "v2",
            "dataset_ref": "custom-fixture",
            "evidence_ref": "local:custom",
            "samples": [
                {
                    "task_type": "custom",
                    "observation_id": str(i),
                    "observed_at": START,
                    "payload": {"checked": True},
                }
                for i in range(2)
            ],
        }
    )
    report = evaluate(manifest, evidence, registry)
    assert report.decision == "PASS"
    assert evidence.samples[0].payload["checked"] is True
