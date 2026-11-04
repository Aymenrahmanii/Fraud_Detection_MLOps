"""Lifecycle replay protection, exact-candidate reviews and bounded machine handoff."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent6_mlops_lifecycle.assurance.worker import artifact_digest
from agent6_mlops_lifecycle.lifecycle.policy import CandidateReport, promotion_decision
from agent6_mlops_lifecycle.lifecycle.settings import LifecycleSettings
from agent6_mlops_lifecycle.lifecycle.store import LifecycleStore
from agent6_mlops_lifecycle.monitoring.app import create_app
from agent6_mlops_lifecycle.monitoring.models import MonitorScope
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings

OPERATOR = "unit-test-operator-not-an-actual-credential-12345"
MACHINE = "unit-test-deployer-not-an-actual-credential-12345"
NOW = datetime(2026, 10, 31, tzinfo=UTC)


def policy(**changes):
    return LifecycleSettings(_env_file=None, **changes)


def test_auto_promotion_cannot_be_enabled_without_calibration():
    with pytest.raises(ValueError, match="calibrated"):
        policy(auto_promote=True)
    assert not policy().auto_promote


def test_jobs_are_durable_idempotent_scoped_and_bounded(tmp_path):
    path = tmp_path / "lifecycle.sqlite3"
    scope = MonitorScope()
    store = LifecycleStore(path, scope)
    config = policy().model_dump(mode="json")
    first = store.schedule("v1", "report-1", config, now=NOW)
    assert first["status"] == "QUEUED"
    assert store.schedule("v1", "report-1", config, now=NOW + timedelta(days=3)) is None
    assert store.schedule("v1", "report-2", config, now=NOW + timedelta(days=3)) is None
    assert LifecycleStore(path, scope).jobs()[0]["job_id"] == first["job_id"]
    assert store.schedule("v2", "report-2", config, now=NOW)
    assert store.schedule("v3", "report-3", config, now=NOW) is None
    with pytest.raises(ValueError, match="another project"):
        LifecycleStore(path, scope.model_copy(update={"project_id": "other"}))


def test_rejected_jobs_respect_cooldown_and_identical_evidence_never_retrains(tmp_path):
    store = LifecycleStore(tmp_path / "jobs.sqlite3", MonitorScope())
    config = policy().model_dump(mode="json")
    job = store.schedule("v1", "report-1", config, now=NOW)
    store.save(job, status="REJECTED")
    assert store.schedule("v1", "report-2", config, now=NOW + timedelta(hours=1)) is None
    assert store.schedule("v1", "report-1", config, now=NOW + timedelta(days=2)) is None
    assert store.schedule("v1", "report-2", config, now=NOW + timedelta(days=2))


def test_stale_job_snapshot_cannot_overwrite_a_concurrent_decision(tmp_path):
    store = LifecycleStore(tmp_path / "jobs.sqlite3", MonitorScope())
    queued = store.schedule("v1", "report", policy().model_dump(mode="json"))
    updated = store.save(queued, status="TRAINING")
    with pytest.raises(ValueError, match="state changed"):
        store.save(queued, status="EXPIRED")
    assert store.evidence.job(queued["job_id"]) == updated


def metrics(f1=0.96):
    tp = round(f1 * 50)
    ratio = tp / 50
    return {
        "f1": ratio,
        "precision": ratio,
        "recall": ratio,
        "balanced_accuracy": ratio,
        "false_positive_rate": (50 - tp) / 50,
        "samples": 100,
        "positive_labels": 50,
        "tp": tp,
        "fp": 50 - tp,
        "fn": 50 - tp,
        "tn": tp,
    }


def evidence(path, digest, *, qualified=True, guard=True, interval=(0.02, 0.2)):
    return {
        "qualified": qualified,
        "improvement_ci95": interval if guard else None,
        "gates": {
            "minimum_f1": qualified,
            "minimum_recall": qualified,
            "false_positive_rate": qualified,
            "historical_regression_guard": True,
            "champion_improvement": True,
            "champion_recall_guard": True,
        },
        "historical": metrics() if guard or qualified else metrics(0.8),
        "candidate_live": metrics() if guard and qualified else metrics(0.8) if guard else None,
        "champion_live": metrics(0.7) if guard else None,
        "verified_live_guard": guard,
        "guard_origin": "VERIFIED_BUSINESS_OUTCOMES" if guard else "HISTORICAL_ONLY",
        "training_source_sha256": "a" * 64,
        "benchmark_dataset_id": "b" * 64,
        "training_independence": "GUARD_FEATURES_EXCLUDED",
        "fit_feature_hashes_sha256": "c" * 64,
        "fit_samples": 500,
        "validation_samples": 100,
        "production_training_rows": 100 if guard else 0,
        "production_guard_rows": 100 if guard else 0,
        "threshold": 0.5,
        "candidate_path": str(path),
        "candidate_sha256": digest,
        "mlflow_run_id": "test-run",
        "registered_model": "fraud-agent6-candidates",
        "registered_version": "1" if qualified else None,
    }


@pytest.mark.parametrize(
    "qualified,guard,interval,automatic,expected",
    [
        (False, True, (0.02, 0.2), False, "REJECTED"),
        (True, False, None, True, "AWAITING_REVIEW"),
        (True, True, (-0.02, 0.1), True, "AWAITING_REVIEW"),
        (True, True, (0.02, 0.2), False, "AWAITING_REVIEW"),
        (True, True, (0.02, 0.2), True, "DEPLOYMENT_READY"),
    ],
)
def test_promotion_uses_measured_support_and_policy(
    qualified, guard, interval, automatic, expected
):
    report = CandidateReport.model_validate(
        evidence(Path("model"), "d" * 64, qualified=qualified, guard=guard, interval=interval)
    )
    selected = policy(auto_promote=automatic, policy_calibrated=automatic)
    assert promotion_decision(report, selected)[0] == expected


def test_report_cannot_claim_qualification_when_a_gate_failed():
    values = evidence(Path("model"), "d" * 64)
    values["gates"]["minimum_recall"] = False
    with pytest.raises(ValueError, match="gates"):
        CandidateReport.model_validate(values)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT6_LIFECYCLE_INTEGRATION_TOKEN", MACHINE)
    monkeypatch.setattr("agent6_mlops_lifecycle.lifecycle.service.ROOT", tmp_path)
    config = MonitoringSettings(
        _env_file=None,
        token=SecretStr(OPERATOR),
        source_database=tmp_path / "source.sqlite3",
        state_database=tmp_path / "state.sqlite3",
        evidence_database=tmp_path / "evidence.sqlite3",
        benchmark_database=tmp_path / "benchmark.sqlite3",
        advisor_enabled=False,
    )
    app = create_app(config, start_worker=False)
    service = app.state.lifecycle
    candidate = tmp_path / "artifacts/agent6/candidates/test/model"
    candidate.mkdir(parents=True)
    (candidate / "model.bin").write_bytes(b"unit-test-owned-artifact")
    digest = artifact_digest(candidate)
    job = service.store.schedule(
        "artifact-previous", "report-id", service.policy.model_dump(mode="json")
    )
    ref = service.store.evidence.put("candidate-evaluation", evidence(candidate, digest))
    job = service.store.save(
        job,
        status="AWAITING_REVIEW",
        candidate_path=str(candidate),
        candidate_sha256=digest,
        evaluation_ref=ref,
        review_expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    )
    return app, service, job, candidate


def test_review_binds_exact_artifact_and_machine_delivery_is_separate(prepared):
    app, service, job, candidate = prepared
    operator = {"Authorization": "Bearer " + OPERATOR}
    machine = {"Authorization": "Bearer " + MACHINE}
    path = f"/api/lifecycle/{job['job_id']}/review"
    body = {
        "decision": "APPROVE",
        "candidate_sha256": job["candidate_sha256"],
        "note": "Independent review evidence",
    }
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers=machine).status_code == 401
        assert (
            client.post(
                path, json={**body, "candidate_sha256": "f" * 64}, headers=operator
            ).status_code
            == 409
        )
        approved = client.post(path, json=body, headers=operator)
        assert approved.status_code == 200
        assert approved.json()["status"] == "DEPLOYMENT_READY"
        assert client.post(path, json=body, headers=operator).status_code == 409
        assert client.get("/api/integrations/deployments", headers=operator).status_code == 401
        delivery = client.get("/api/integrations/deployments", headers=machine)
        assert delivery.status_code == 200
        item = delivery.json()["requests"][0]
        assert item["kind"] == "MODEL_DEPLOYMENT_REQUEST"
        assert item["candidate_sha256"] == job["candidate_sha256"]
        assert item["preconditions"]["active_model_version"] == "artifact-previous"
        result = {
            "status": "SUCCEEDED",
            "candidate_sha256": job["candidate_sha256"],
            "model_version": "artifact-" + job["candidate_sha256"],
            "evidence_ref": "deployment-test-evidence",
        }
        endpoint = f"/api/integrations/deployments/{job['job_id']}/result"
        assert client.post(endpoint, json=result, headers=operator).status_code == 401
        assert (
            client.post(
                endpoint, json={**result, "model_version": "wrong-version"}, headers=machine
            ).status_code
            == 409
        )
        assert client.post(endpoint, json=result, headers=machine).json()["status"] == "DEPLOYED"
        assert client.post(endpoint, json=result, headers=machine).status_code == 200
        assert (
            client.post(
                endpoint, json={**result, "evidence_ref": "changed"}, headers=machine
            ).status_code
            == 409
        )
        assert client.get("/api/integrations/deployments", headers=machine).json()["requests"] == []


def test_tampered_candidate_cannot_be_approved(prepared):
    app, service, job, candidate = prepared
    (candidate / "model.bin").write_bytes(b"tampered")
    with TestClient(app) as client:
        response = client.post(
            f"/api/lifecycle/{job['job_id']}/review",
            json={
                "decision": "APPROVE",
                "candidate_sha256": job["candidate_sha256"],
                "note": "Attempt after tampering",
            },
            headers={"Authorization": "Bearer " + OPERATOR},
        )
        assert response.status_code == 409
    assert service.store.evidence.job(job["job_id"])["status"] == "AWAITING_REVIEW"


def test_expired_and_rejected_reviews_do_not_deploy(prepared):
    app, service, job, candidate = prepared
    job = service.store.save(
        job, review_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/lifecycle/{job['job_id']}/review",
            json={
                "decision": "APPROVE",
                "candidate_sha256": job["candidate_sha256"],
                "note": "Expired approval",
            },
            headers={"Authorization": "Bearer " + OPERATOR},
        )
        assert response.status_code == 409
    service.store.save(job, review_expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    rejected = service.store.resolve(
        job["job_id"], "REJECT", job["candidate_sha256"], "operator", "Reject based on evidence"
    )
    assert rejected["status"] == "REJECTED"
    assert rejected["review_ref"]


@pytest.mark.parametrize("fault", ["expired", "tampered"])
def test_deployment_acknowledgment_rechecks_authorization_and_artifact(prepared, fault):
    app, service, job, candidate = prepared
    changes = {"status": "DEPLOYMENT_READY"}
    if fault == "expired":
        changes["review_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    else:
        (candidate / "model.bin").write_bytes(b"changed-after-delivery")
    service.store.save(job, **changes)
    with TestClient(app) as client:
        response = client.post(
            f"/api/integrations/deployments/{job['job_id']}/result",
            json={
                "action": "DEPLOY",
                "status": "SUCCEEDED",
                "candidate_sha256": job["candidate_sha256"],
                "model_version": "artifact-" + job["candidate_sha256"],
                "evidence_ref": "consumer-check",
            },
            headers={"Authorization": "Bearer " + MACHINE},
        )
    assert response.status_code == 409
    assert service.store.evidence.job(job["job_id"])["status"] == "DEPLOYMENT_READY"


def test_metrics_cannot_disagree_with_confusion_counts():
    from agent6_mlops_lifecycle.lifecycle.policy import GuardMetrics

    values = metrics()
    values["f1"] = 1.0
    with pytest.raises(ValueError, match="confusion counts"):
        GuardMetrics.model_validate(values)


def test_worker_cannot_relax_configured_promotion_gates():
    report = CandidateReport.model_validate(evidence(Path("model"), "d" * 64))
    strict = policy(min_f1=0.99)
    with pytest.raises(ValueError, match="configured promotion policy"):
        promotion_decision(report, strict)


def test_float_roundoff_cannot_leak_guard_feature_groups():
    from agent6_mlops_lifecycle.lifecycle.data import feature_fingerprint

    row = dict(
        accountAgeDays=1,
        numItems=2,
        localTime=4.965339,
        paymentMethod="paypal",
        paymentMethodAgeDays=2.564583333,
        Category="shopping",
        isWeekend=0.0,
    )
    assert feature_fingerprint(row) == feature_fingerprint({**row, "localTime": 4.965338999999999})
    assert feature_fingerprint(row) != feature_fingerprint({**row, "accountAgeDays": 400})
    with pytest.raises(ValueError, match="Nonfinite"):
        feature_fingerprint({**row, "localTime": float("inf")})


@pytest.mark.parametrize(
    "changes,eligible",
    [
        ({"Category": None, "isWeekend": None}, True),
        ({"accountAgeDays": None}, False),
        ({"paymentMethod": None}, False),
        ({"localTime": float("inf")}, False),
        ({"isWeekend": 3}, False),
        ({"numItems": -1}, False),
    ],
)
def test_training_eligibility_respects_nullable_serving_fields(changes, eligible):
    from agent6_mlops_lifecycle.lifecycle.data import eligible_training_inputs

    row = dict(
        accountAgeDays=1,
        numItems=2,
        localTime=4.965339,
        paymentMethod="paypal",
        paymentMethodAgeDays=2.564583333,
        Category="shopping",
        isWeekend=0.0,
    )
    assert eligible_training_inputs({**row, **changes}) is eligible
    assert not eligible_training_inputs({})


def test_verification_fixtures_never_allow_automatic_promotion():
    data = evidence(Path("model"), "d" * 64)
    data["guard_origin"] = "ISOLATED_VERIFICATION_FIXTURES"
    report = CandidateReport.model_validate(data)
    assert (
        promotion_decision(report, policy(auto_promote=True, policy_calibrated=True))[0]
        == "AWAITING_REVIEW"
    )


def test_deployed_regression_creates_one_rollback_handoff(prepared, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    app, service, job, candidate = prepared
    service.store.save(job, status="DEPLOYED")
    monkeypatch.setattr(
        service.monitor,
        "latest",
        lambda version: SimpleNamespace(
            evaluation=SimpleNamespace(decision="FAIL"),
            operational_health=None,
            report_id="regression-evidence",
        ),
    )
    asyncio.run(service.cycle())
    record = service.store.evidence.job(job["job_id"])
    assert record["status"] == "ROLLBACK_REQUESTED"
    request = service.store.evidence.get("rollback-request", record["rollback_ref"])
    assert request["restore_model_version"] == job["model_version"]
    assert request["evidence_ref"] == "regression-evidence"
    asyncio.run(service.cycle())
    assert service.store.evidence.job(job["job_id"])["rollback_ref"] == record["rollback_ref"]


def test_recent_verified_guard_never_shares_feature_groups_with_training():
    from agent6_mlops_lifecycle.lifecycle.data import feature_fingerprint, partition_verified_rows

    rows = []
    for index in range(120):
        inputs = dict(
            accountAgeDays=index % 100,
            numItems=2,
            localTime=4.0,
            paymentMethod="paypal",
            paymentMethodAgeDays=0.0,
            Category="shopping",
            isWeekend=0.0,
        )
        rows.append(
            {
                "observation_id": str(index),
                "observed_at": f"{index:04}",
                "inputs": inputs,
                "actual": index % 2,
                "predicted": 0,
            }
        )
    fit, guard = partition_verified_rows(rows)
    assert len(fit) + len(guard) == len(rows)
    assert len(guard) >= 50
    assert not {feature_fingerprint(row["inputs"]) for row in fit} & {
        feature_fingerprint(row["inputs"]) for row in guard
    }


def test_failed_rollout_requests_rollback_and_acknowledges_restoration(prepared):
    app, service, job, candidate = prepared
    operator = {"Authorization": "Bearer " + OPERATOR}
    machine = {"Authorization": "Bearer " + MACHINE}
    with TestClient(app) as client:
        client.post(
            f"/api/lifecycle/{job['job_id']}/review",
            json={
                "decision": "APPROVE",
                "candidate_sha256": job["candidate_sha256"],
                "note": "Reviewed exact candidate",
            },
            headers=operator,
        ).raise_for_status()
        endpoint = f"/api/integrations/deployments/{job['job_id']}/result"
        failed = {
            "status": "FAILED",
            "candidate_sha256": job["candidate_sha256"],
            "model_version": "artifact-" + job["candidate_sha256"],
            "evidence_ref": "failed-readiness-evidence",
        }
        result = client.post(endpoint, json=failed, headers=machine)
        assert result.json()["status"] == "ROLLBACK_REQUESTED"
        assert client.post(endpoint, json=failed, headers=machine).status_code == 200
        assert (
            client.get("/api/integrations/deployments", headers=machine).json()["requests"][0][
                "kind"
            ]
            == "MODEL_ROLLBACK_REQUEST"
        )
        restored = {
            **failed,
            "action": "ROLLBACK",
            "status": "SUCCEEDED",
            "model_version": job["model_version"],
            "evidence_ref": "restore-evidence",
        }
        assert (
            client.post(endpoint, json=restored, headers=machine).json()["status"] == "ROLLED_BACK"
        )


def test_metrics_export_does_not_expose_operator_or_machine_credentials(prepared):
    app, service, job, candidate = prepared
    with TestClient(app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "agent6_worker_up" in response.text
        assert OPERATOR not in response.text and MACHINE not in response.text
