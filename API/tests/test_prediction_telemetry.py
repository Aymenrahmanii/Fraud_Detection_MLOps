"""Serving evidence, persistence, concurrency and trustworthy snapshot boundaries."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from API import services
from API.main import app
from API.telemetry.config import TelemetryConfig
from API.telemetry.contracts import PredictionEvent, Scope
from API.telemetry.export import export_predictions
from API.telemetry.store import PredictionLogError, SQLitePredictionLog

PAYLOAD = {
    "paymentMethod": "paypal",
    "numItems": 1,
    "localTime": 4.7,
    "paymentMethodAgeDays": 0,
    "accountAgeDays": 1,
}


def rows(log):
    with sqlite3.connect(log.database) as connection:
        return connection.execute("SELECT payload FROM predictions").fetchall()


def snapshot(log, output, **kwargs):
    now = datetime.now(UTC)
    return export_predictions(
        database=log.database,
        output_dir=output,
        scope=log.scope,
        window_start=now - timedelta(hours=1),
        window_end=now,
        as_of=now,
        **kwargs,
    )


def test_success_retains_response_body_and_records_joinable_identity(
    isolate_prediction_log, monkeypatch
):
    monkeypatch.setattr(services.model, "predict", lambda frame: [1])
    with TestClient(app) as client:
        response = client.post("/predict", json=PAYLOAD)
    assert response.status_code == 200 and response.json() == {"prediction": 1}
    event = PredictionEvent.model_validate_json(rows(isolate_prediction_log)[0][0])
    assert str(UUID(response.headers["X-Observation-ID"])) == event.observation_id
    assert response.headers["X-Model-Version"] == "artifact-" + "a" * 64
    assert event.recorded_at >= event.observed_at
    assert event.predicted == 1
    assert event.source_ref == "sha256:" + "a" * 64
    assert not {"label", "actual", "features", "paymentMethod"} & set(
        event.model_dump()
    )


def test_invalid_input_and_failed_inference_do_not_create_events(
    isolate_prediction_log, monkeypatch
):
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/predict", json={}).status_code == 422
        monkeypatch.setattr(services.model, "predict", lambda frame: [0.5])
        assert client.post("/predict", json=PAYLOAD).status_code == 500
    assert rows(isolate_prediction_log) == []


def test_recording_failure_returns_503_without_exposing_source_or_success_id(
    isolate_prediction_log, monkeypatch
):
    def failure(*args, **kwargs):
        raise PredictionLogError("private storage details")

    monkeypatch.setattr(isolate_prediction_log, "append", failure)
    with TestClient(app) as client:
        response = client.post("/predict", json=PAYLOAD)
    assert response.status_code == 503
    assert response.json() == {"detail": "Prediction recording is unavailable"}
    assert "X-Observation-ID" not in response.headers


def test_reopen_and_export_leave_outcomes_pending(isolate_prediction_log, tmp_path):
    log = isolate_prediction_log
    first = log.append(1, datetime.now(UTC))
    reopened = SQLitePredictionLog(log.database, log.scope, "a" * 64)
    second = reopened.append(0, datetime.now(UTC))
    result = snapshot(reopened, tmp_path / "snapshot")
    output = tmp_path / "snapshot"
    events = [
        PredictionEvent.model_validate_json(line)
        for line in (output / "predictions.jsonl").read_bytes().splitlines()
    ]
    assert {event.observation_id for event in events} == {
        first.observation_id,
        second.observation_id,
    }
    assert result["outcome_count"] == 0
    assert (output / "outcomes.jsonl").read_bytes() == b""
    assert (
        result["prediction_file_sha256"]
        == sha256((output / "predictions.jsonl").read_bytes()).hexdigest()
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["model_version"] == log.model_version
    assert manifest["project_id"] == "fraud-detection-serving"


def test_concurrent_connections_preserve_every_successful_prediction(
    isolate_prediction_log,
):
    log = isolate_prediction_log
    other = SQLitePredictionLog(log.database, log.scope, "a" * 64)

    def append(index):
        return (log if index % 2 else other).append(index % 2, datetime.now(UTC))

    with ThreadPoolExecutor(max_workers=8) as workers:
        records = list(workers.map(append, range(40)))
    assert len({record.observation_id for record in records}) == 40
    assert len(rows(log)) == 40


def test_mixed_versions_require_an_explicit_selection(isolate_prediction_log, tmp_path):
    log = isolate_prediction_log
    log.append(1, datetime.now(UTC))
    second = SQLitePredictionLog(log.database, log.scope, "b" * 64)
    second.append(0, datetime.now(UTC))
    with pytest.raises(ValueError, match="multiple model versions"):
        snapshot(log, tmp_path / "mixed")
    selected = snapshot(log, tmp_path / "selected", model_version=second.model_version)
    assert selected["prediction_count"] == 1


def test_export_filters_tenant_and_project_scope(isolate_prediction_log, tmp_path):
    log = isolate_prediction_log
    log.append(1, datetime.now(UTC))
    other = SQLitePredictionLog(
        log.database, Scope(tenant_id="another-tenant"), "a" * 64
    )
    other.append(0, datetime.now(UTC))
    result = snapshot(log, tmp_path / "scoped")
    assert result["prediction_count"] == 1
    event = json.loads((tmp_path / "scoped/predictions.jsonl").read_text())
    assert event["tenant_id"] == "local-fraud"


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE predictions SET digest='invalid'",
        "UPDATE predictions SET payload=zeroblob(4097)",
        "UPDATE predictions SET observed_at='2000-01-01T00:00:00.000000+00:00'",
    ],
)
def test_corrupted_or_mismatched_records_are_not_exported(
    isolate_prediction_log, tmp_path, mutation
):
    log = isolate_prediction_log
    log.append(1, datetime.now(UTC))
    with sqlite3.connect(log.database) as connection:
        connection.execute(mutation)
    # The altered index is outside this window; that case returns no matches.
    with pytest.raises((PredictionLogError, ValueError)):
        snapshot(log, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_snapshot_does_not_overwrite_an_existing_export(
    isolate_prediction_log, tmp_path
):
    log = isolate_prediction_log
    log.append(1, datetime.now(UTC))
    output = tmp_path / "snapshot"
    snapshot(log, output)
    original = (output / "predictions.jsonl").read_bytes()
    with pytest.raises(FileExistsError):
        snapshot(log, output)
    assert (output / "predictions.jsonl").read_bytes() == original


def test_unrelated_database_is_rejected_without_changing_its_tables(tmp_path):
    database = tmp_path / "other.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
    with pytest.raises(PredictionLogError):
        SQLitePredictionLog(database, Scope(), "a" * 64)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("example",)]


def test_missing_snapshot_database_is_not_created(tmp_path):
    database = tmp_path / "missing.sqlite3"
    now = datetime.now(UTC)
    with pytest.raises(FileNotFoundError):
        export_predictions(
            database=database,
            output_dir=tmp_path / "out",
            scope=Scope(),
            window_start=now - timedelta(hours=1),
            window_end=now,
            as_of=now,
        )
    assert not database.exists()


@pytest.mark.parametrize("value", [True, 0.5, 2, "1"])
def test_invalid_binary_values_never_enter_the_log(isolate_prediction_log, value):
    with pytest.raises(ValueError):
        isolate_prediction_log.append(value, datetime.now(UTC))
    assert rows(isolate_prediction_log) == []


def test_clock_reversal_does_not_create_a_record(tmp_path):
    now = datetime.now(UTC)
    log = SQLitePredictionLog(
        tmp_path / "pred.sqlite3", Scope(), "a" * 64, clock=lambda: now
    )
    with pytest.raises(ValueError):
        log.append(1, now + timedelta(seconds=1))
    assert rows(log) == []


def test_environment_overrides_producer_local_file(monkeypatch, tmp_path):
    from API.telemetry import config

    monkeypatch.setattr(
        config, "dotenv_values", lambda path: {"AGENT6_PROJECT_ID": "from-file"}
    )
    monkeypatch.setenv("AGENT6_PROJECT_ID", "from-env")
    monkeypatch.setenv(
        "AGENT6_PREDICTION_DATABASE", str(tmp_path / "configured.sqlite3")
    )
    value = TelemetryConfig.from_env()
    assert value.scope.project_id == "from-env"
    assert value.database == (tmp_path / "configured.sqlite3").resolve()
