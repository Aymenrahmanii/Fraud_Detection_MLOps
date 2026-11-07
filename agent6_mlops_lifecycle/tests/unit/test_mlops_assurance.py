"""Benchmark isolation, immutable evidence, deterministic gates and advisor boundaries."""

import asyncio
import csv
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agent6_mlops_lifecycle.assurance import advisor, benchmark
from agent6_mlops_lifecycle.assurance.catalog import FEATURES, import_fraud_holdout
from agent6_mlops_lifecycle.assurance.models import (
    BenchmarkDataset,
    BenchmarkRegistry,
    BenchmarkRow,
    ModelArtifact,
)
from agent6_mlops_lifecycle.assurance.service import AssuranceService
from agent6_mlops_lifecycle.assurance.store import AssuranceStore
from agent6_mlops_lifecycle.assurance.worker import artifact_digest
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.state import MonitorState

NOW = datetime(2026, 10, 22, tzinfo=UTC)


@pytest.fixture
def configured(tmp_path):
    config = MonitoringSettings(
        _env_file=None,
        token=SecretStr("test-assurance-operator-credential-12345"),
        source_database=tmp_path / "serving.sqlite3",
        state_database=tmp_path / "state.sqlite3",
        evidence_database=tmp_path / "evidence.sqlite3",
        benchmark_database=tmp_path / "assurance.sqlite3",
        benchmark_registry=tmp_path / "registry.json",
        benchmark_python=Path("python.exe"),
        enable_service_probes=False,
    )
    store = AssuranceStore(config.benchmark_database, config.scope())
    dataset = BenchmarkDataset(
        source_name="project-holdout.csv",
        source_sha256="a" * 64,
        rows=tuple(
            BenchmarkRow(inputs={"value": index}, actual=label, group="A" if index < 2 else "B")
            for index, label in enumerate([0, 1, 0, 1])
        ),
    )
    dataset_id = store.put("dataset", dataset.model_dump(mode="json"))
    artifact = tmp_path / "model.pkl"
    artifact.write_bytes(b"trusted-test-artifact-not-a-pickle")
    spec = ModelArtifact(name="Test model", kind="joblib", path=artifact)
    registry = BenchmarkRegistry(project_root=tmp_path, dataset_id=dataset_id, models=(spec,))
    config.benchmark_registry.write_text(registry.model_dump_json(), encoding="utf-8")
    return config, store, registry, spec


def test_catalog_is_immutable_scoped_and_detects_tampering(configured):
    config, store, registry, _ = configured
    assert store.get("dataset", registry.dataset_id)["training_independence"] == "UNVERIFIED"
    with pytest.raises(ValueError, match="another project"):
        AssuranceStore(
            config.benchmark_database, config.scope().model_copy(update={"project_id": "another"})
        )
    with sqlite3.connect(config.benchmark_database) as connection:
        connection.execute(
            "UPDATE documents SET payload=? WHERE id=?", (b"{}", registry.dataset_id)
        )
    with pytest.raises(ValueError, match="digest"):
        store.get("dataset", registry.dataset_id)


def test_foreign_database_is_rejected(tmp_path):
    config = MonitoringSettings(_env_file=None)
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE transactions (id TEXT)")
    with pytest.raises(ValueError, match="supported"):
        AssuranceStore(path, config.scope())


def test_catalog_import_preserves_labels_without_creating_serving_records(configured, tmp_path):
    config, store, _, _ = configured
    path = tmp_path / "holdout.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*FEATURES, "label", "dataset"])
        writer.writeheader()
        for index in range(4):
            writer.writerow(
                dict(
                    accountAgeDays=index,
                    numItems=1,
                    localTime=4.5,
                    paymentMethod="paypal",
                    paymentMethodAgeDays=0.0,
                    Category="",
                    isWeekend=0.0,
                    label=index % 2,
                    dataset="A" if index < 2 else "B",
                )
            )
    identity = import_fraud_holdout(path, store)
    assert import_fraud_holdout(path, store) == identity
    dataset = BenchmarkDataset.model_validate(store.get("dataset", identity))
    assert [row.actual for row in dataset.rows] == [0, 1, 0, 1]
    assert dataset.rows[0].inputs["Category"] is None
    assert dataset.source_sha256 == sha256(path.read_bytes()).hexdigest()
    assert not config.source_database.exists()


@pytest.mark.parametrize("replacement", ["2,A", "true,A", "1,UNKNOWN"])
def test_catalog_rejects_invalid_labels_and_groups(configured, tmp_path, replacement):
    _, store, _, _ = configured
    path = tmp_path / "holdout.csv"
    path.write_text(
        ",".join([*FEATURES, "label", "dataset"])
        + "\n1,1,4.5,paypal,0,shopping,0,"
        + replacement
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        import_fraud_holdout(path, store)


def test_actual_metrics_and_small_group_limitations_are_preserved(configured):
    config, store, registry, spec = configured
    report = asyncio.run(
        benchmark.evaluate(
            config, store, registry, spec, artifact_digest(spec.path), [0, 0, 0, 1], NOW
        )
    )
    result = report.evaluations["ALL"]
    assert result.metrics["binary.precision.v1"] == 1
    assert result.metrics["binary.recall.v1"] == 0.5
    assert result.metrics["binary.f1.v1"] == pytest.approx(2 / 3)
    assert result.decision == "FAIL"
    assert report.production_correctness == "UNKNOWN"
    assert report.training_independence == "UNVERIFIED"
    assert report.evaluations["A"].sample_count == 2
    assert report.evaluations["B"].sample_count == 2
    with pytest.raises(ValueError, match="align"):
        asyncio.run(
            benchmark.evaluate(config, store, registry, spec, artifact_digest(spec.path), [0], NOW)
        )
    assert not config.source_database.exists()


def test_scheduler_survives_reopen_and_reacts_to_policy_and_artifact_changes(
    configured, monkeypatch
):
    config, store, registry, spec = configured
    calls = []

    async def infer(*args):
        calls.append(args[-1])
        return [0, 0, 0, 1]

    monkeypatch.setattr(benchmark, "infer", infer)
    asyncio.run(benchmark.run_benchmarks(config, store))
    asyncio.run(benchmark.run_benchmarks(config, AssuranceStore(store.path, config.scope())))
    assert len(calls) == 1
    changed_policy = config.model_copy(update={"min_quality_score": 0.6})
    asyncio.run(benchmark.run_benchmarks(changed_policy, store))
    assert len(calls) == 2
    spec.path.write_bytes(b"different-model")
    asyncio.run(benchmark.run_benchmarks(config, store))
    assert len(calls) == 3
    assert len(store.jobs("benchmark")) == 3
    assert all(job["status"] == "COMPLETED" for job in store.jobs("benchmark"))


def test_failed_inference_is_visible_and_retry_is_bounded(configured, monkeypatch):
    config, store, _, _ = configured
    calls = []

    async def fail(*args):
        calls.append(1)
        raise TimeoutError("test timeout, no secret")

    monkeypatch.setattr(benchmark, "infer", fail)
    asyncio.run(benchmark.run_benchmarks(config, store))
    asyncio.run(benchmark.run_benchmarks(config, store))
    assert len(calls) == 1
    assert store.jobs("benchmark")[0]["status"] == "ERROR"
    assert "test timeout" not in store.jobs("benchmark")[0]["error"]
    assert not config.source_database.exists()


def test_artifact_escape_is_rejected_before_inference(configured, tmp_path):
    config, store, registry, spec = configured
    registry = registry.model_copy(update={"project_root": tmp_path / "different-project"})
    registry.project_root.mkdir()
    config.benchmark_registry.write_text(registry.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(benchmark.run_benchmarks(config, store))


def response(reference="sha256:example"):
    return {
        "summary": "Live correctness is unknown without confirmed outcomes.",
        "findings": [
            {"text": "Benchmark independence is unverified.", "evidence_ids": [reference]}
        ],
        "recommended_checks": ["Obtain training provenance."],
        "limitations": ["No verified production outcomes."],
    }


def test_advisor_sends_only_given_aggregates_and_validates_citations(configured, monkeypatch):
    config, _, _, _ = configured
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.host == "api.groq.com"
        assert "unit-test-key" not in request.content.decode()
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "production correctness is UNKNOWN" in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(plan_response())}}
                ]
            },
        )

    result = asyncio.run(
        advisor.explain(
            config,
            {
                "sha256:example": {
                    "kind": "LABELED_BENCHMARK",
                    "training_independence": "UNVERIFIED",
                    "f1": 0.8,
                }
            },
            transport=httpx.MockTransport(handler),
        )
    )
    assert result.findings[0].evidence_ids == ("sha256:example",)
    assert len(seen) == 1
    assert "inputs" not in json.dumps(seen)


@pytest.mark.parametrize(
    "mode", ["unknown_reference", "incomplete", "refusal", "bad_json", "rate_limit"]
)
def test_advisor_failure_never_becomes_a_measured_decision(configured, monkeypatch, mode):
    config, _, _, _ = configured
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")

    def handler(request):
        if mode == "rate_limit":
            return httpx.Response(429, text="Sensitive provider diagnostic")
        content = json.dumps(
            response("sha256:invented" if mode == "unknown_reference" else "sha256:example")
        )
        message = {"content": "not-json" if mode == "bad_json" else content}
        if mode == "refusal":
            message["refusal"] = "refused"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if mode == "incomplete" else "stop",
                        "message": message,
                    }
                ]
            },
        )

    with pytest.raises(RuntimeError, match="measured monitoring continues"):
        asyncio.run(
            advisor.explain(
                config, {"sha256:example": {"f1": 0.8}}, transport=httpx.MockTransport(handler)
            )
        )


def test_advisor_context_excludes_raw_transactions_and_cannot_claim_live_quality(
    configured, monkeypatch
):
    config, store, _, _ = configured

    async def infer(*args):
        return [0, 0, 0, 1]

    monkeypatch.setattr(benchmark, "infer", infer)
    asyncio.run(benchmark.run_benchmarks(config, store))
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    context = service.context()
    assert context
    assert all(value["production_correctness"] == "UNKNOWN" for value in context.values())
    assert "inputs" not in json.dumps(context)
    assert "predictions" not in json.dumps(context)
    assert service.snapshot()["advisor"]["status"] == "DISABLED"


def test_corrupt_assurance_does_not_break_serving_status(configured):
    config, _, registry, _ = configured
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    service.store.save_job(
        "test", "benchmark", NOW, {"status": "COMPLETED", "report_ref": registry.dataset_id}
    )
    assert service.snapshot()["error"]
    assert service.snapshot()["advisor"]["status"] == "ERROR"


def test_benchmark_database_cannot_be_live_database(configured):
    config, _, _, _ = configured
    with pytest.raises(ValueError, match="separate"):
        MonitoringSettings(
            _env_file=None,
            benchmark_database=config.source_database,
            source_database=config.source_database,
        )


def test_advisor_rate_limit_survives_evidence_changes(configured, monkeypatch):
    config, _, _, _ = configured
    config = config.model_copy(update={"advisor_enabled": True})
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    monkeypatch.setattr(
        service,
        "context",
        lambda: {
            "sha256:example": {"kind": "LIVE_MONITOR", "quality_decision": "UNKNOWN", "f1": 0.8}
        },
    )
    calls = []

    async def explain(*args):
        calls.append(1)
        return advisor.AdvisorExplanation.model_validate(response())

    monkeypatch.setattr("agent6_mlops_lifecycle.assurance.service.explain", explain)
    asyncio.run(service.advise())
    monkeypatch.setattr(
        service,
        "context",
        lambda: {
            "sha256:changed": {"kind": "LIVE_MONITOR", "quality_decision": "UNKNOWN", "f1": 0.5}
        },
    )
    asyncio.run(service.advise())
    assert len(calls) == 1
    assert service.snapshot()["advisor"]["status"] == "COMPLETED"


def test_interrupted_job_recovers_after_timeout_instead_of_waiting_a_full_hour(
    configured, monkeypatch
):
    config, store, _, _ = configured
    calls = []

    async def infer(*args):
        calls.append(1)
        return [0, 0, 0, 1]

    monkeypatch.setattr(benchmark, "infer", infer)
    asyncio.run(benchmark.run_benchmarks(config, store))
    with sqlite3.connect(store.path) as connection:
        key = connection.execute("SELECT id FROM jobs WHERE kind='benchmark'").fetchone()[0]
    checkpoint = store.job(key)
    checkpoint["status"] = "RUNNING"
    store.save_job(
        key,
        "benchmark",
        datetime.now(UTC) - timedelta(seconds=config.benchmark_timeout_seconds + 20),
        checkpoint,
    )
    asyncio.run(benchmark.run_benchmarks(config, store))
    assert len(calls) == 2
    assert store.job(key)["status"] == "COMPLETED"


def test_missing_key_is_visible_and_makes_no_external_request(configured, monkeypatch):
    config, _, _, _ = configured
    config = config.model_copy(update={"advisor_enabled": True})
    monkeypatch.setenv("GROQ_API_KEY", "")
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    monkeypatch.setattr(
        "agent6_mlops_lifecycle.assurance.service.explain",
        lambda *args: pytest.fail("No LLM call without a key"),
    )
    asyncio.run(service.advise())
    assert service.snapshot()["advisor"]["status"] == "NOT_CONFIGURED"


def test_successful_explanation_is_not_billed_again_for_unchanged_evidence(configured, monkeypatch):
    config, _, _, _ = configured
    config = config.model_copy(update={"advisor_enabled": True})
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    monkeypatch.setattr(
        service,
        "context",
        lambda: {
            "sha256:example": {"kind": "LIVE_MONITOR", "quality_decision": "UNKNOWN", "f1": 0.8}
        },
    )
    calls = []

    async def explain(*args):
        calls.append(1)
        return advisor.AdvisorExplanation.model_validate(response())

    monkeypatch.setattr("agent6_mlops_lifecycle.assurance.service.explain", explain)
    asyncio.run(service.advise())
    with sqlite3.connect(service.store.path) as connection:
        key = connection.execute("SELECT id FROM jobs WHERE kind='advisor'").fetchone()[0]
    checkpoint = service.store.job(key)
    service.store.save_job(key, "advisor", datetime.now(UTC) - timedelta(hours=1), checkpoint)
    asyncio.run(service.advise())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "envelope", [[], {"choices": {}}, {"choices": [{"finish_reason": "stop", "message": []}]}]
)
def test_malformed_provider_envelopes_are_contained(configured, monkeypatch, envelope):
    config, _, _, _ = configured
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=envelope))
    with pytest.raises(RuntimeError, match="measured monitoring continues"):
        asyncio.run(advisor.explain(config, {"sha256:example": {"f1": 0.8}}, transport=transport))


def test_context_identifies_distinct_models_and_unknown_drift(configured, monkeypatch):
    config, store, _, _ = configured

    async def infer(*args):
        return [0, 0, 0, 1]

    monkeypatch.setattr(benchmark, "infer", infer)
    asyncio.run(benchmark.run_benchmarks(config, store))
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    context = service.context()
    assert all(value["artifact_name"] == "Test model" for value in context.values())
    assert all(value["training_independence"] == "UNVERIFIED" for value in context.values())
    prompt = advisor._TEMPLATE.read_text(encoding="utf-8")
    assert "DIFFERENT saved model artifacts" in prompt
    assert "drift is UNKNOWN" in prompt


def plan_response(reference="sha256:example"):
    return {"findings": [{"check_id": "VERIFY_TRAINING_INDEPENDENCE", "evidence_ids": [reference]}]}


def test_advisor_cannot_invent_a_service_failure_or_rewrite_gate_decisions():
    from agent6_mlops_lifecycle.assurance.interpretation import render_plan
    from agent6_mlops_lifecycle.assurance.models import AdvisoryPlan

    evidence = {
        "sha256:example": {
            "kind": "LABELED_BENCHMARK",
            "artifact_name": "API",
            "model_version": "version-one",
            "training_independence": "UNVERIFIED",
            "groups": {
                "ALL": {"decision": "PASS", "sample_count": 300},
                "A": {"decision": "FAIL", "sample_count": 100},
            },
        }
    }
    plan = AdvisoryPlan.model_validate(plan_response())
    result = render_plan(evidence, plan)
    assert "ALL=PASS" in result.summary
    assert "ALL=PASS" in result.findings[0].text
    unsupported = AdvisoryPlan.model_validate(
        {"findings": [{"check_id": "CHECK_SERVICE_HEALTH", "evidence_ids": ["sha256:example"]}]}
    )
    with pytest.raises(ValueError, match="unsupported"):
        render_plan(evidence, unsupported)


def test_scripted_outcomes_are_not_rendered_as_live_correctness():
    from agent6_mlops_lifecycle.assurance.interpretation import render_plan
    from agent6_mlops_lifecycle.assurance.models import AdvisoryPlan

    evidence = {
        "sha256:fixture": {
            "kind": "LIVE_MONITOR",
            "evidence_origin": "SCRIPTED_VERIFICATION_FIXTURES",
            "model_version": "test-version",
            "quality_decision": "FAIL",
            "policy_is_calibrated": False,
            "drift": {"status": "DETECTED"},
        }
    }
    plan = AdvisoryPlan.model_validate(
        {
            "findings": [
                {"check_id": "REVIEW_DISTRIBUTION_SHIFT", "evidence_ids": ["sha256:fixture"]}
            ]
        }
    )
    result = render_plan(evidence, plan)
    assert "SCRIPTED fixture" in result.summary
    assert "Scripted fixture model" in result.findings[0].text
    assert "do not establish production" in result.limitations[0]


def test_healthy_service_only_does_not_trigger_unsupported_llm_request(configured, monkeypatch):
    config, _, _, _ = configured
    config = config.model_copy(update={"advisor_enabled": True})
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")
    service = AssuranceService(config, MonitorState(config.state_database, config.scope()))
    monkeypatch.setattr(
        service,
        "context",
        lambda: {
            "sha256:service": {"kind": "CURRENT_SERVICE_HEALTH", "services": {"fraud-api": "UP"}}
        },
    )
    monkeypatch.setattr(
        "agent6_mlops_lifecycle.assurance.service.explain",
        lambda *args: pytest.fail("No request without an eligible investigation"),
    )
    asyncio.run(service.advise())
    assert service.store.jobs("advisor") == []
