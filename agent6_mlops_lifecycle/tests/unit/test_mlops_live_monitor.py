"""File-free monitoring, attributed outcomes, replay, drift, auth and worker recovery."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from agent6_mlops_lifecycle.contracts.mlops_ingestion import PredictionRecord
from agent6_mlops_lifecycle.monitoring.app import create_app
from agent6_mlops_lifecycle.monitoring.engine import MonitoringEngine, WorkerLeaseLostError
from agent6_mlops_lifecycle.monitoring.models import (
    DriftBaseline,
    MonitorScope,
    OutcomeSubmission,
)
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    OutcomeConflictError,
    OutcomeNotFoundError,
    SourceUnavailableError,
    canonical,
    utc,
)
from agent6_mlops_lifecycle.monitoring.state import MonitorState, MonitorStateError

NOW = datetime(2026, 10, 21, 10, 0, 10, tzinfo=UTC)
VERSION = "artifact-" + "a" * 64
TOKEN = "unit-test-local-monitor-credential-not-a-real-secret"


@pytest.mark.parametrize(
    "health_url,expected",
    [
        (None, None),
        ("http://127.0.0.1:8521/_stcore/health?private=value", "http://127.0.0.1:8521/"),
        ("http://localhost:8856/_stcore/health", "http://localhost:8856/"),
        ("https://[::1]:8856/_stcore/health", "https://[::1]:8856/"),
        ("http://fraud-ui:8521/_stcore/health", None),
    ],
)
def test_dashboard_application_link_preserves_public_origin_only(tmp_path, health_url, expected):
    settings = configured(
        tmp_path, application_health_url=health_url, allowed_serving_hosts=("fraud-ui",)
    )
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = client.get("/api/status", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    assert response.json()["configuration"]["application_url"] == expected


def configured(tmp_path, **overrides):
    settings = MonitoringSettings(
        _env_file=None,
        token=SecretStr(TOKEN),
        source_database=tmp_path / "source.sqlite3",
        state_database=tmp_path / "state.sqlite3",
        evidence_database=tmp_path / "evidence.sqlite3",
        interval_seconds=1,
        quality_window_seconds=overrides.get("window_seconds", 3600),
        baseline_mode="manual",
        enable_service_probes=False,
        manual_review_enabled=True,
        min_samples=6,
        min_positive_samples=1,
        min_negative_samples=1,
        min_distribution_samples=6,
        **overrides,
    )
    with sqlite3.connect(settings.source_database) as connection:
        connection.execute("PRAGMA application_id=1094078546")
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "CREATE TABLE predictions (observation_id TEXT PRIMARY KEY,tenant_id TEXT,"
            "project_id TEXT,model_id TEXT,model_version TEXT,observed_at TEXT,"
            "payload BLOB,digest TEXT)"
        )
    return settings


def prediction(settings, value=0, *, version=VERSION, observed=None, recorded=None, scope=None):
    scope = scope or settings.scope()
    event = PredictionRecord(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        model_id=scope.model_id,
        model_version=version,
        task_type="binary_classification",
        observation_id=str(uuid4()),
        source_ref="sha256:" + "a" * 64,
        observed_at=observed or NOW - timedelta(seconds=2),
        recorded_at=recorded or NOW - timedelta(seconds=1),
        predicted=value,
    )
    raw = canonical(event)
    with sqlite3.connect(settings.source_database) as connection:
        connection.execute(
            "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?)",
            (
                event.observation_id,
                event.tenant_id,
                event.project_id,
                event.model_id,
                event.model_version,
                utc(event.observed_at),
                raw,
                sha256(raw).hexdigest(),
            ),
        )
    return event


def components(settings, clock=None):
    source = DirectObservationSource(
        settings.source_database, settings.scope(), clock or (lambda: NOW)
    )
    state = MonitorState(settings.state_database, settings.scope())
    engine = MonitoringEngine(settings, source, state, clock or (lambda: NOW))
    return source, state, engine


def submission(actual=0, kind="HUMAN", note="Independently reviewed case"):
    return OutcomeSubmission(actual=actual, verified=True, note=note, kind=kind)


def test_source_application_id_constant():
    from agent6_mlops_lifecycle.monitoring.source import SOURCE_APPLICATION_ID

    assert SOURCE_APPLICATION_ID == 1094078546


def test_direct_source_to_real_agent_task_then_immutable_replay(tmp_path):
    settings = configured(tmp_path)
    events = [prediction(settings, value) for value in (0, 0, 1, 1, 0, 1, 0, 1)]
    source, state, engine = components(settings)
    asyncio.run(engine.cycle())
    pending = state.latest(VERSION)
    assert pending.task_status == "NOT_RUN" and pending.evaluation is None
    assert pending.ingestion.summary.outcome_coverage == 0
    assert len(pending.ingestion.summary.pending_observation_ids) == 8
    for event, actual in zip(events, (0, 1, 1, 0, 0, 1, 0, 1), strict=True):
        source.submit_outcome(event.observation_id, submission(actual), "trusted-reviewer")
    asyncio.run(engine.cycle())
    report = state.latest(VERSION)
    assert report.task_status == "SUCCEEDED"
    assert report.evaluation.decision == "FAIL"
    assert report.evaluation.metrics["binary.f1.v1"] == pytest.approx(0.75)
    assert report.human_outcomes == 8 and report.simulated_outcomes == 0
    assert report.ingestion.reference is not None
    assert state.alerts()[0]["kind"] == "QUALITY_FAILURE"
    total = len(state.reports())
    asyncio.run(engine.cycle())
    assert len(state.reports()) == total
    reopened = MonitorState(settings.state_database, settings.scope())
    assert reopened.latest(VERSION) == report
    with sqlite3.connect(settings.evidence_database) as connection:
        assert connection.execute("SELECT count(*) FROM evaluation_batches").fetchone()[0] == 1


def test_real_outcome_approval_enables_psi_without_retraining(tmp_path):
    settings = configured(tmp_path)
    for value in (0, 0, 0, 0, 1, 1, 1, 1):
        prediction(settings, value)
    source, state, engine = components(settings)
    asyncio.run(engine.cycle())
    frame = source.read_frames(NOW - timedelta(hours=1), NOW, NOW)[0]
    baseline = DriftBaseline(
        scope=settings.scope(),
        model_version=VERSION,
        prediction_counts=(4, 4),
        prediction_source_ref=frame.snapshot.prediction_source_ref,
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        approved_at=NOW,
        approved_by="operator",
        note="Reviewed reference window",
    )
    state.approve_baseline(baseline)
    for _ in range(8):
        prediction(settings, 1)
    asyncio.run(engine.cycle())
    report = state.latest(VERSION)
    assert report.drift.status == "DETECTED" and report.drift.score > 0.2
    assert report.evaluation is None
    assert report.recommendation == "REVIEW_SHIFT"
    assert report.automatic_deployment is False
    assert any(row["kind"] == "PREDICTION_SHIFT" for row in state.alerts())
    with pytest.raises(ValueError, match="already exists"):
        state.approve_baseline(baseline.model_copy(update={"note": "A different reference"}))


def test_delayed_answers_respect_cutoff_and_old_evidence(tmp_path):
    settings = configured(tmp_path)
    event = prediction(settings)
    source, _, _ = components(settings, lambda: NOW + timedelta(seconds=10))
    source.submit_outcome(event.observation_id, submission(), "operator")
    early = source.read_frames(NOW - timedelta(hours=1), NOW, NOW)[0]
    late = source.read_frames(NOW - timedelta(hours=1), NOW, NOW + timedelta(seconds=11))[0]
    assert early.snapshot.outcomes == ()
    assert len(late.snapshot.outcomes) == 1
    assert early.snapshot.outcome_source_ref != late.snapshot.outcome_source_ref


def test_duplicate_feedback_is_idempotent_and_conflicting_feedback_is_rejected(tmp_path):
    settings = configured(tmp_path)
    event = prediction(settings)
    source, _, _ = components(settings)
    first = source.submit_outcome(event.observation_id, submission(), "operator")
    assert source.submit_outcome(event.observation_id, submission(), "operator") == first
    for review, actor in ((submission(1), "operator"), (submission(), "someone-else")):
        with pytest.raises(OutcomeConflictError):
            source.submit_outcome(event.observation_id, review, actor)


def test_wrong_project_cannot_be_reviewed_or_seen(tmp_path):
    settings = configured(tmp_path)
    event = prediction(settings, scope=MonitorScope(tenant_id="other"))
    source, _, engine = components(settings)
    assert source.read_frames(NOW - timedelta(hours=1), NOW, NOW) == ()
    with pytest.raises(OutcomeNotFoundError):
        source.submit_outcome(event.observation_id, submission(), "operator")
    asyncio.run(engine.cycle())
    assert engine.state.status().state == "WAITING_FOR_PREDICTIONS"


def test_source_corruption_fails_closed(tmp_path):
    settings = configured(tmp_path)
    prediction(settings)
    source, _, _ = components(settings)
    with sqlite3.connect(settings.source_database) as connection:
        connection.execute("UPDATE predictions SET digest='invalid'")
    with pytest.raises(SourceUnavailableError, match="integrity"):
        source.read_frames(NOW - timedelta(hours=1), NOW, NOW)


def test_expired_window_does_not_keep_reporting_healthy_current_data(tmp_path):
    settings = configured(tmp_path)
    prediction(settings)
    clock = [NOW]
    _, state, engine = components(settings, lambda: clock[0])
    asyncio.run(engine.cycle())
    assert state.status().active_versions == (VERSION,)
    clock[0] += timedelta(hours=2)
    asyncio.run(engine.cycle())
    assert state.status().state == "WAITING_FOR_PREDICTIONS"
    assert state.status().active_versions == ()


def test_lease_prevents_two_monitors_and_allows_expired_owner_recovery(tmp_path):
    settings = configured(tmp_path)
    _, state, first = components(settings)
    _, _, second = components(settings)
    asyncio.run(first.cycle())
    with pytest.raises(WorkerLeaseLostError):
        asyncio.run(second.cycle())
    assert state.acquire(second.owner, NOW + timedelta(seconds=61), 60)


def test_state_cannot_be_reused_for_another_scope(tmp_path):
    settings = configured(tmp_path)
    MonitorState(settings.state_database, settings.scope())
    with pytest.raises(MonitorStateError, match="different project"):
        MonitorState(settings.state_database, MonitorScope(tenant_id="other"))


@pytest.mark.parametrize(
    "changes",
    [
        {"actual": True},
        {"actual": 0.5},
        {"verified": False},
        {"verified": 1},
        {"reviewer_id": "forged"},
    ],
)
def test_invalid_feedback_cannot_forge_identity_or_verification(changes):
    raw = {"actual": 0, "verified": True, "note": "Reviewed case", **changes}
    with pytest.raises(ValidationError):
        OutcomeSubmission.model_validate(raw)


def test_dashboard_auth_guards_reads_and_writes(tmp_path):
    settings = configured(tmp_path)
    event = prediction(
        settings,
        observed=datetime.now(UTC) - timedelta(seconds=2),
        recorded=datetime.now(UTC) - timedelta(seconds=1),
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/predictions").status_code == 401
        assert (
            client.post(
                "/api/outcomes/" + event.observation_id, json=submission().model_dump()
            ).status_code
            == 401
        )
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.get("/api/status", headers=headers)
        assert response.status_code == 200 and TOKEN not in response.text
        assert response.headers["X-Frame-Options"] == "DENY"
        result = client.post(
            "/api/outcomes/" + event.observation_id, headers=headers, json=submission().model_dump()
        )
        assert result.status_code == 200
        assert result.json()["audit"]["reviewer_id"] == settings.reviewer_id
        simulated = client.post(
            "/api/outcomes/" + str(uuid4()),
            headers=headers,
            json=submission(kind="SIMULATED").model_dump(),
        )
        assert simulated.status_code == 403
        assert (
            client.post(
                "/api/outcomes/" + str(uuid4()), headers=headers, json=submission().model_dump()
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/outcomes/" + event.observation_id,
                headers=headers,
                json=submission(1).model_dump(),
            ).status_code
            == 409
        )


def test_unconfigured_token_and_mixed_demo_scope_are_rejected(tmp_path):
    settings = configured(tmp_path)
    with pytest.raises(ValueError, match="token"):
        create_app(settings.model_copy(update={"token": SecretStr("")}))
    with pytest.raises(ValidationError):
        MonitoringSettings(_env_file=None, demo_mode=True)


def test_async_worker_recovers_after_source_becomes_available(tmp_path):
    settings = configured(tmp_path)
    event = prediction(settings)
    source, state, engine = components(settings)
    original = source.read_frames
    attempts = [0]

    def flaky(*args):
        attempts[0] += 1
        if attempts[0] == 1:
            raise SourceUnavailableError("Temporary source failure")
        return original(*args)

    source.read_frames = flaky

    async def exercise():
        task = asyncio.create_task(engine.run())
        for _ in range(50):
            await asyncio.sleep(0.03)
            heartbeat = state.status()
            if heartbeat and heartbeat.state == "ERROR":
                break
        assert state.status().state == "ERROR"
        engine.wake.set()
        for _ in range(50):
            await asyncio.sleep(0.03)
            if state.latest(VERSION):
                break
        assert state.latest(VERSION).ingestion.summary.pending_observation_ids == (
            event.observation_id,
        )
        engine.stop()
        await task

    asyncio.run(exercise())
    assert state.status().state == "STOPPED"


def test_api_baseline_requires_actual_sufficient_reference_and_is_immutable(tmp_path):
    settings = configured(tmp_path)
    # API uses its live clock; these records belong to the real current window.
    now = datetime.now(UTC)
    for index in range(6):
        prediction(
            settings,
            index % 2,
            observed=now - timedelta(seconds=3),
            recorded=now - timedelta(seconds=2),
        )
    app = create_app(settings, start_worker=False)
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        body = {"model_version": VERSION, "note": "Reviewed representative reference"}
        assert client.post("/api/baselines", headers=headers, json=body).status_code == 200
        assert client.post("/api/baselines", headers=headers, json=body).status_code == 409
        assert client.post("/api/baselines", json=body).status_code == 401


def test_oversized_and_foreign_database_records_fail_closed(tmp_path):
    settings = configured(tmp_path)
    prediction(settings)
    source, _, _ = components(settings)
    with sqlite3.connect(settings.source_database) as connection:
        connection.execute("UPDATE predictions SET payload=zeroblob(5000)")
    with pytest.raises(SourceUnavailableError, match="byte limit"):
        source.read_frames(NOW - timedelta(hours=1), NOW, NOW)
    with sqlite3.connect(settings.source_database) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(SourceUnavailableError, match="supported"):
        source.read_frames(NOW - timedelta(hours=1), NOW, NOW)


def test_source_predictions_cannot_be_confirmed_before_they_happen(tmp_path):
    settings = configured(tmp_path)
    event = prediction(
        settings, observed=NOW + timedelta(seconds=10), recorded=NOW + timedelta(seconds=11)
    )
    source, _, _ = components(settings)
    with pytest.raises(ValueError, match="precedes"):
        source.submit_outcome(event.observation_id, submission(), "operator")


def test_monitor_refuses_internet_proxy_and_database_path_aliases(tmp_path):
    with pytest.raises(ValidationError, match="loopback"):
        MonitoringSettings(_env_file=None, prediction_url="http://example.com/predict")
    with pytest.raises(ValidationError, match="separate"):
        MonitoringSettings(
            _env_file=None,
            source_database=tmp_path / "same.db",
            state_database=tmp_path / "same.db",
        )


def test_token_setup_preserves_existing_agent_environment(tmp_path, monkeypatch):
    import agent6_mlops_lifecycle.cli.monitor as cli

    original = b"AGENT6_LLM_PROVIDER=groq\nGROQ_API_KEY=unit-test-not-a-real-key\n"
    env = tmp_path / ".env"
    env.write_bytes(original)
    monkeypatch.setattr(cli, "ENV_FILE", env)
    monkeypatch.setattr(cli, "MonitoringSettings", lambda: MonitoringSettings(_env_file=env))
    first = cli.ensure_monitor_token()
    assert env.read_bytes().startswith(original)
    assert len(first.token.get_secret_value()) >= 32
    contents = env.read_bytes()
    second = cli.ensure_monitor_token()
    assert second.token == first.token and env.read_bytes() == contents


def test_prediction_listing_keeps_ui_and_api_artifact_versions_separate(tmp_path):
    settings = configured(tmp_path)
    now = datetime.now(UTC)
    for version in (VERSION, "artifact-" + "b" * 64):
        prediction(
            settings,
            version=version,
            observed=now - timedelta(seconds=3),
            recorded=now - timedelta(seconds=2),
        )
    with TestClient(create_app(settings, start_worker=False)) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        all_rows = client.get("/api/predictions", headers=headers)
        assert len(all_rows.json()) == 2
        selected = client.get(
            "/api/predictions", params={"model_version": VERSION}, headers=headers
        )
        assert selected.status_code == 200
        assert len(selected.json()) == 1 and selected.json()[0]["model_version"] == VERSION
