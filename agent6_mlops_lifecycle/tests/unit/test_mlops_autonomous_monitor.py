"""Automatic monitoring with no human reviews and explicit business-event provenance."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent6_mlops_lifecycle.contracts.mlops_ingestion import PredictionRecord
from agent6_mlops_lifecycle.integrations.outcomes import OutcomePublisher
from agent6_mlops_lifecycle.monitoring.app import create_app
from agent6_mlops_lifecycle.monitoring.autonomy import service_probes
from agent6_mlops_lifecycle.monitoring.engine import MonitoringEngine
from agent6_mlops_lifecycle.monitoring.operations import read_operations
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    SourceUnavailableError,
    canonical,
    utc,
)
from agent6_mlops_lifecycle.monitoring.state import MonitorState
from agent6_mlops_lifecycle.monitoring.telemetry_models import FEATURE_NAMES, ServingEvent

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
VERSION = "artifact-" + "a" * 64
OPERATOR = "unit-test-operator-not-a-real-secret-credential"
SERVICE = "unit-test-outcome-service-not-a-real-credential"


@pytest.fixture
def setup(tmp_path):
    config = MonitoringSettings(
        _env_file=None,
        token=SecretStr(OPERATOR),
        outcome_token=SecretStr(SERVICE),
        source_database=tmp_path / "serving.sqlite3",
        state_database=tmp_path / "state.sqlite3",
        evidence_database=tmp_path / "evidence.sqlite3",
        min_distribution_samples=4,
        baseline_warmup_seconds=0,
        min_operational_samples=2,
        min_samples=4,
        min_positive_samples=1,
        min_negative_samples=1,
        enable_service_probes=False,
    )
    with sqlite3.connect(config.source_database) as c:
        c.execute("PRAGMA application_id=1094078546")
        c.execute("PRAGMA user_version=1")
        c.execute(
            "CREATE TABLE predictions (observation_id TEXT PRIMARY KEY,tenant_id TEXT,"
            "project_id TEXT,model_id TEXT,model_version TEXT,observed_at TEXT,"
            "payload BLOB,digest TEXT)"
        )
        c.execute(
            "CREATE TABLE agent6_serving_events_v1 (event_id TEXT PRIMARY KEY,tenant_id TEXT,"
            "project_id TEXT,model_id TEXT,model_version TEXT,observed_at TEXT,"
            "payload BLOB,digest TEXT)"
        )
    return config


def emit(config, value=0, *, at=None, bad=False, failure=False, latency=10):
    at = at or NOW - timedelta(seconds=5)
    identity = str(uuid4())
    bins = {name: "bin-1" for name in FEATURE_NAMES}
    bins.update(paymentMethod="paypal", Category="shopping", isWeekend="0")
    if bad:
        bins["Category"] = "INVALID"
    event = ServingEvent(
        **config.scope().model_dump(),
        event_id=str(uuid4()),
        model_version=VERSION,
        observation_id=None if failure else identity,
        observed_at=at,
        recorded_at=at,
        channel="streamlit",
        duration_ms=latency,
        success=not failure,
        status_code=500 if failure else 200,
        feature_bins=bins,
    )
    with sqlite3.connect(config.source_database) as c:
        if not failure:
            pred = PredictionRecord(
                **config.scope().model_dump(),
                model_version=VERSION,
                task_type="binary_classification",
                observation_id=identity,
                source_ref="sha256:" + "a" * 64,
                observed_at=at,
                recorded_at=at,
                predicted=value,
            )
            raw = canonical(pred)
            c.execute(
                "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    config.tenant_id,
                    config.project_id,
                    config.model_id,
                    VERSION,
                    utc(at),
                    raw,
                    sha256(raw).hexdigest(),
                ),
            )
        raw = canonical(event)
        c.execute(
            "INSERT INTO agent6_serving_events_v1 VALUES(?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                config.tenant_id,
                config.project_id,
                config.model_id,
                VERSION,
                utc(at),
                raw,
                sha256(raw).hexdigest(),
            ),
        )
    return identity


def engine(config, clock=lambda: NOW):
    source = DirectObservationSource(config.source_database, config.scope(), clock)
    state = MonitorState(config.state_database, config.scope())
    return MonitoringEngine(config, source, state, clock)


def test_reference_freezes_and_later_shift_alerts_without_labels_or_clicks(setup):
    clock = [NOW]
    for i in range(4):
        emit(setup, i % 2, at=NOW - timedelta(seconds=20 - i))
    worker = engine(setup, lambda: clock[0])
    asyncio.run(worker.cycle())
    reference = worker.state.baseline(VERSION)
    assert reference.origin == "AUTO_WARMUP"
    assert all(sum(counts.values()) == 4 for counts in reference.feature_counts.values())
    assert worker.state.latest(VERSION).drift.current_samples == 0
    for i in range(4):
        emit(setup, 1, at=NOW + timedelta(seconds=i + 1), bad=True)
    clock[0] = NOW + timedelta(seconds=10)
    asyncio.run(worker.cycle())
    report = worker.state.latest(VERSION)
    assert report.drift.current_samples == 4
    assert report.drift.status == "DETECTED"
    assert report.feature_drift.status == "DETECTED"
    assert "Category" in report.feature_drift.detected_features
    assert report.evaluation is None and report.human_outcomes == 0
    assert report.recommendation == "REVIEW_OPERATIONS"
    assert worker.state.baseline(VERSION) == reference
    alerts = len(worker.state.alerts())
    asyncio.run(worker.cycle())
    assert len(worker.state.alerts()) == alerts
    assert {a["kind"] for a in worker.state.alerts()} >= {
        "INPUT_QUALITY",
        "FEATURE_SHIFT",
        "PREDICTION_SHIFT",
    }
    worker.state.release(worker.owner)
    restarted = engine(setup, lambda: clock[0])
    asyncio.run(restarted.cycle())
    assert restarted.state.baseline(VERSION) == reference
    assert len(restarted.state.alerts()) == alerts


def test_failure_only_stream_is_monitored_without_any_predictions(setup):
    for _ in range(2):
        emit(setup, failure=True, bad=True, latency=2000)
    worker = engine(setup)
    asyncio.run(worker.cycle())
    status = worker.state.status()
    health = status.operations[VERSION]
    assert health.status == "DEGRADED" and health.attempts == 2
    assert health.failure_rate == 1 and health.latency_p95_ms == 2000
    assert set(health.reasons) == {"ERROR_RATE_HIGH", "INPUT_QUALITY", "LATENCY_HIGH"}
    assert not status.active_versions


def test_no_labels_does_not_prevent_healthy_operational_checks(setup):
    for i in range(2):
        emit(setup, i)
    worker = engine(setup)
    asyncio.run(worker.cycle())
    report = worker.state.latest(VERSION)
    assert report.operational_health.status == "HEALTHY"
    assert report.operational_health.telemetry_coverage == 1
    assert report.evaluation is None
    assert report.system_outcomes == report.human_outcomes == 0


def test_tampered_operational_evidence_fails_closed(setup):
    emit(setup)
    with sqlite3.connect(setup.source_database) as c:
        c.execute("UPDATE agent6_serving_events_v1 SET digest='broken'")
    with pytest.raises(SourceUnavailableError, match="integrity"):
        read_operations(
            DirectObservationSource(setup.source_database, setup.scope()),
            NOW - timedelta(hours=1),
            NOW,
        )


def event_body(identity, actual=0, *, event_id=None, occurred=None):
    return dict(
        event_id=str(event_id or uuid4()),
        observation_id=identity,
        model_version=VERSION,
        actual=actual,
        evidence_ref="business-case:resolved-123",
        occurred_at=(occurred or NOW - timedelta(seconds=1)).isoformat(),
    )


def test_service_and_operator_credentials_are_separate_and_manual_is_disabled(setup):
    identity = emit(setup)
    with TestClient(create_app(setup, start_worker=False)) as client:
        path = "/api/integrations/outcomes"
        assert client.post(path, json=event_body(identity)).status_code == 401
        assert (
            client.post(
                path, json=event_body(identity), headers={"Authorization": "Bearer " + OPERATOR}
            ).status_code
            == 401
        )
        assert (
            client.get("/api/status", headers={"Authorization": "Bearer " + SERVICE}).status_code
            == 401
        )
        manual = dict(actual=0, note="independent evidence", verified=True)
        assert (
            client.post(
                "/api/outcomes/" + identity,
                json=manual,
                headers={"Authorization": "Bearer " + OPERATOR},
            ).status_code
            == 403
        )
        response = client.get("/api/status", headers={"Authorization": "Bearer " + OPERATOR})
        assert SERVICE not in response.text and OPERATOR not in response.text


def test_system_event_is_idempotent_and_reuse_or_conflict_is_rejected(setup):
    first, second = emit(setup), emit(setup)
    app = create_app(setup, start_worker=False)
    app.state.source._clock = lambda: NOW
    headers = {"Authorization": "Bearer " + SERVICE}
    with TestClient(app) as client:
        body = event_body(first)
        one = client.post("/api/integrations/outcomes", json=body, headers=headers)
        assert one.status_code == 200
        assert (
            client.post("/api/integrations/outcomes", json=body, headers=headers).json()
            == one.json()
        )
        assert (
            client.post(
                "/api/integrations/outcomes", json={**body, "actual": 1}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/integrations/outcomes",
                json={**body, "observation_id": second},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/integrations/outcomes",
                json=event_body(second, occurred=NOW + timedelta(days=30)),
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/integrations/outcomes",
                json={**event_body(second), "model_version": "wrong"},
                headers=headers,
            ).status_code
            == 404
        )


def test_delayed_business_events_trigger_quality_evaluation_without_human_review(setup):
    identities = [emit(setup, i % 2, at=NOW - timedelta(days=3, seconds=i)) for i in range(4)]
    app = create_app(setup, start_worker=False)
    app.state.source._clock = lambda: NOW
    with TestClient(app) as client:
        for i, identity in enumerate(identities):
            response = client.post(
                "/api/integrations/outcomes",
                json=event_body(identity, i % 2),
                headers={"Authorization": "Bearer " + SERVICE},
            )
            assert response.status_code == 200
    worker = engine(setup)
    asyncio.run(worker.cycle())
    report = worker.state.latest(VERSION)
    assert report.system_outcomes == 4 and report.human_outcomes == report.simulated_outcomes == 0
    assert report.evaluation.decision == "PASS"
    assert report.evaluation.metrics["binary.f1.v1"] == 1


def test_service_health_probes_use_health_routes_and_never_predict(setup, monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 2 else 200)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    settings = setup.model_copy(
        update={
            "enable_service_probes": True,
            "application_health_url": "http://127.0.0.1:8501/_stcore/health",
        }
    )
    results = asyncio.run(service_probes(settings))
    assert results["fraud-api"].state == "UP"
    assert results["fraud-application"].state == "DOWN"
    assert all(r.method == "GET" and "predict" not in r.url.path for r in requests)


def test_publisher_retries_identical_event_after_transient_failure(monkeypatch):
    requests = []
    original = httpx.Client

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503 if len(requests) == 1 else 200)

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs)
    )
    monkeypatch.setattr("agent6_mlops_lifecycle.integrations.outcomes.time.sleep", lambda _: None)
    publisher = OutcomePublisher("http://127.0.0.1:8012", SERVICE)
    publisher.publish(
        event_id=uuid4(),
        observation_id=uuid4(),
        model_version=VERSION,
        actual=0,
        evidence_ref="resolved-case:123",
        occurred_at=NOW,
    )
    assert len(requests) == 2 and requests[0] == requests[1]


def make_business_database(tmp_path, config, events):
    database = tmp_path / "business.sqlite3"
    with sqlite3.connect(database) as c:
        c.execute(
            "CREATE TABLE agent6_business_outcomes_v1 (sequence_id INTEGER PRIMARY KEY,"
            "event_id TEXT,observation_id TEXT,model_version TEXT,actual INTEGER,"
            "evidence_ref TEXT,occurred_at TEXT,tenant_id TEXT,project_id TEXT,model_id TEXT)"
        )
        for sequence, event in enumerate(events, 1):
            c.execute(
                "INSERT INTO agent6_business_outcomes_v1 VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    sequence,
                    event["event_id"],
                    event["observation_id"],
                    event["model_version"],
                    event["actual"],
                    event["evidence_ref"],
                    event["occurred_at"],
                    config.tenant_id,
                    config.project_id,
                    config.model_id,
                ),
            )
    return database


def test_database_events_are_read_automatically_and_restart_keeps_checkpoint(setup, tmp_path):
    from agent6_mlops_lifecycle.monitoring.outcome_feed import poll_outcomes

    identities = [emit(setup, i % 2) for i in range(4)]
    database = make_business_database(
        tmp_path, setup, [event_body(identity, i % 2) for i, identity in enumerate(identities)]
    )
    config = setup.model_copy(
        update={"business_database_url": SecretStr("sqlite:///" + str(database))}
    )
    worker = engine(config)
    asyncio.run(worker.cycle())
    report = worker.state.latest(VERSION)
    assert report.system_outcomes == 4 and report.evaluation.decision == "PASS"
    assert worker.state.status().outcome_feed.state == "CONNECTED"
    assert worker.state.status().outcome_feed.last_sequence == 4
    reopened = MonitorState(config.state_database, config.scope())
    status = poll_outcomes(config, worker.source, reopened)
    assert status.last_sequence == 4 and status.processed_this_poll == 0
    with sqlite3.connect(database) as c:
        assert c.execute("SELECT count(*) FROM agent6_business_outcomes_v1").fetchone()[0] == 4


def test_unmatched_database_event_is_retained_and_resumes_when_prediction_arrives(setup, tmp_path):
    from agent6_mlops_lifecycle.monitoring.outcome_feed import poll_outcomes

    identity = emit(setup)
    body = event_body(str(uuid4()))
    database = make_business_database(tmp_path, setup, [body])
    config = setup.model_copy(
        update={"business_database_url": SecretStr("sqlite:///" + str(database))}
    )
    worker = engine(config)
    status = poll_outcomes(config, worker.source, worker.state)
    assert status.state == "WAITING_FOR_PREDICTION" and status.last_sequence == 0
    with sqlite3.connect(database) as c:
        c.execute("UPDATE agent6_business_outcomes_v1 SET observation_id=?", (identity,))
    status = poll_outcomes(config, worker.source, worker.state)
    assert status.state == "CONNECTED" and status.last_sequence == 1


def test_database_failure_is_visible_without_disabling_serving_checks(setup, tmp_path):
    emit(setup)
    config = setup.model_copy(
        update={
            "business_database_url": SecretStr("sqlite:///" + str(tmp_path / "missing.sqlite3"))
        }
    )
    worker = engine(config)
    asyncio.run(worker.cycle())
    status = worker.state.status()
    assert status.state == "MONITORING" and status.outcome_feed.state == "ERROR"
    assert "missing.sqlite3" not in status.outcome_feed.error
    assert not (tmp_path / "missing.sqlite3").exists()


def test_invalid_database_mapping_is_rejected_before_sql_execution():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="SQL identifier"):
        MonitoringSettings(_env_file=None, business_outcome_table="events; DROP TABLE predictions")


def test_legacy_manual_audit_digest_remains_readable(setup):
    # Optional machine metadata must not change the canonical bytes of old human audits.
    from agent6_mlops_lifecycle.monitoring.models import OutcomeSubmission

    submission = OutcomeSubmission(actual=0, note="Verified original human review", verified=True)
    payload = submission.model_dump(mode="json")
    assert set(payload) == {"actual", "note", "verified", "kind"}


def test_service_failure_and_recovery_alerts_are_transition_based(setup):
    from agent6_mlops_lifecycle.monitoring.telemetry_models import ServiceProbe

    state = MonitorState(setup.state_database, setup.scope())
    down = {"fraud-api": ServiceProbe(state="DOWN", checked_at=NOW, error="Unavailable")}
    up = {
        "fraud-api": ServiceProbe(state="UP", checked_at=NOW + timedelta(seconds=1), response_ms=1)
    }
    state.save_services(down, NOW)
    state.save_services(down, NOW)
    assert len(state.alerts()) == 1
    state.save_services(up, NOW + timedelta(seconds=1))
    state.save_services(up, NOW + timedelta(seconds=1))
    assert {alert["kind"] for alert in state.alerts()} == {"SERVICE_DOWN", "SERVICE_RECOVERED"}
