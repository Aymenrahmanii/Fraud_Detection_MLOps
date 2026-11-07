"""Real storage verifies atomic telemetry, rejection capture and model failures."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from API import services
from API.main import app
from API.telemetry.operations import bucket_features

PAYLOAD = {
    "Category": "shopping",
    "paymentMethod": "paypal",
    "isWeekend": 0,
    "numItems": 2,
    "localTime": 4.7,
    "paymentMethodAgeDays": 0,
    "accountAgeDays": 30,
}


def records(log):
    with sqlite3.connect(log.database) as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM agent6_serving_events_v1"
            )
        ]


def test_success_operation_matches_prediction_and_contains_no_raw_unknown_fields(
    isolate_prediction_log,
):
    with TestClient(app) as client:
        response = client.post(
            "/predict", json={**PAYLOAD, "customer_secret": "not-to-be-recorded"}
        )
    assert response.status_code == 200
    event = records(isolate_prediction_log)[0]
    assert event["observation_id"] == response.headers["X-Observation-ID"]
    assert event["success"] and event["duration_ms"] >= 0
    assert "not-to-be-recorded" not in json.dumps(event)
    with sqlite3.connect(isolate_prediction_log.database) as connection:
        prediction = json.loads(
            connection.execute("SELECT payload FROM predictions").fetchone()[0]
        )
    assert event["observed_at"] == prediction["observed_at"]


def test_invalid_request_records_failure_and_no_fake_prediction(isolate_prediction_log):
    with TestClient(app) as client:
        response = client.post("/predict", json={**PAYLOAD, "numItems": "bad"})
    assert response.status_code == 422
    event = records(isolate_prediction_log)[0]
    assert not event["success"] and event["status_code"] == 422
    assert event["feature_bins"]["numItems"] == "INVALID"
    with sqlite3.connect(isolate_prediction_log.database) as connection:
        assert connection.execute("SELECT count(*) FROM predictions").fetchone()[0] == 0


def test_model_exception_records_failed_attempt(isolate_prediction_log, monkeypatch):
    def broken(frame):
        raise RuntimeError("private model diagnostics")

    monkeypatch.setattr(services.model, "predict", broken)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/predict", json=PAYLOAD)
    assert response.status_code == 500
    event = records(isolate_prediction_log)[0]
    assert not event["success"] and event["observation_id"] is None
    assert "private model diagnostics" not in json.dumps(event)


def test_prediction_rolls_back_if_its_success_telemetry_cannot_be_stored(
    isolate_prediction_log, monkeypatch
):
    def broken(*args, **kwargs):
        raise ValueError("operational storage failure")

    monkeypatch.setattr("API.telemetry.operations.insert_operation", broken)
    with pytest.raises(ValueError):
        isolate_prediction_log.append(
            0, datetime.now(UTC), duration_ms=5, inputs=PAYLOAD
        )
    with sqlite3.connect(isolate_prediction_log.database) as connection:
        assert connection.execute("SELECT count(*) FROM predictions").fetchone()[0] == 0


def test_invalid_values_become_bounded_buckets_and_never_raw_payloads():
    bins = bucket_features(
        {
            **PAYLOAD,
            "numItems": True,
            "localTime": float("nan"),
            "paymentMethod": "unexpected-private-value",
        }
    )
    assert bins["numItems"] == bins["localTime"] == bins["paymentMethod"] == "INVALID"
    assert "unexpected-private-value" not in json.dumps(bins)
