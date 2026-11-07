"""Business truth correlation, delayed delivery, isolation and immutable resolutions."""

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from API.main import app
from API.telemetry.business_events import BusinessEvent, receive_event
from API.telemetry.contracts import Scope
from API.telemetry.store import SQLitePredictionLog

FEATURES = {
    "Category": "shopping",
    "paymentMethod": "paypal",
    "isWeekend": 0.0,
    "numItems": 1,
    "localTime": 4.7,
    "paymentMethodAgeDays": 0.0,
    "accountAgeDays": 1,
}
KEY = "test-business-service-credential-at-least-32-characters"


def event(transaction_id="order-1", **changes):
    return BusinessEvent(
        event_id=uuid4(),
        transaction_id=transaction_id,
        source_id="business-service",
        status="FRAUD_CONFIRMED",
        verification_status="VERIFIED",
        evidence_ref="independent-case-123",
        occurred_at=datetime.now(UTC),
        **changes,
    )


def confirmations(log):
    with sqlite3.connect(log.database) as db:
        return db.execute(
            "SELECT observation_id,model_version,actual FROM resolved_outcomes"
        ).fetchall()


def test_business_id_resolves_each_exact_model_and_identical_event_is_idempotent(
    tmp_path,
):
    first = SQLitePredictionLog(tmp_path / "app.sqlite3", Scope(), "a" * 64)
    second = SQLitePredictionLog(first.database, Scope(), "b" * 64)
    for log in (first, second):
        log.append(
            1,
            datetime.now(UTC),
            inputs=FEATURES
            if log is first
            else {**FEATURES, "isWeekend": 0, "accountAgeDays": 1.0},
            transaction_id="order-1",
            positive_probability=0.8,
            decision_threshold=0.5,
        )
    body = event()
    for _ in range(2):
        with first._connect() as db:
            result = receive_event(db, first.scope, body, ("business-service",))
        assert result["matched_observations"] == 2
    assert len(confirmations(first)) == 2
    assert {row[1] for row in confirmations(first)} == {
        "artifact-" + "a" * 64,
        "artifact-" + "b" * 64,
    }


def test_delayed_prediction_capture_resolves_durable_pending_event(tmp_path):
    log = SQLitePredictionLog(tmp_path / "app.sqlite3", Scope(), "a" * 64)
    observed = datetime.now(UTC) - timedelta(seconds=2)
    body = event()
    with log._connect() as db:
        assert (
            receive_event(db, log.scope, body, ("business-service",))["status"]
            == "WAITING_FOR_PREDICTION"
        )
    reopened = SQLitePredictionLog(log.database, Scope(), "a" * 64)
    prediction = reopened.append(0, observed, inputs=FEATURES, transaction_id="order-1")
    assert confirmations(log) == [(prediction.observation_id, log.model_version, 1)]


def test_confirmation_cannot_be_used_to_label_a_future_prediction(tmp_path):
    log = SQLitePredictionLog(tmp_path / "app.sqlite3", Scope(), "a" * 64)
    with log._connect() as db:
        receive_event(db, log.scope, event(), ("business-service",))
    log.append(1, datetime.now(UTC), inputs=FEATURES, transaction_id="order-1")
    assert confirmations(log) == []


def test_different_inputs_cannot_reuse_a_business_id(tmp_path):
    log = SQLitePredictionLog(tmp_path / "app.sqlite3", Scope(), "a" * 64)
    log.append(0, datetime.now(UTC), inputs=FEATURES, transaction_id="order-1")
    with pytest.raises(ValueError, match="rebound"):
        log.append(
            1,
            datetime.now(UTC),
            inputs={**FEATURES, "accountAgeDays": 999},
            transaction_id="order-1",
        )
    with sqlite3.connect(log.database) as db:
        assert db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1


def test_business_scope_and_conflicting_confirmations_are_enforced(tmp_path):
    log = SQLitePredictionLog(tmp_path / "app.sqlite3", Scope(), "a" * 64)
    log.append(1, datetime.now(UTC), inputs=FEATURES, transaction_id="order-1")
    body = event()
    with log._connect() as db:
        receive_event(db, Scope(tenant_id="other"), body, ("business-service",))
    assert not confirmations(log)
    with log._connect() as db:
        receive_event(db, log.scope, body, ("business-service",))
    with pytest.raises(ValueError, match="Conflicting"), log._connect() as db:
        receive_event(
            db,
            log.scope,
            body.model_copy(update={"status": "LEGITIMATE_CONFIRMED"}),
            ("business-service",),
        )
    assert confirmations(log)[0][2] == 1


@pytest.mark.parametrize(
    "fault", ["credential", "publisher", "unverified", "nonfinal", "future"]
)
def test_http_business_feed_rejects_untrusted_or_nonfinal_truth(
    isolate_prediction_log, monkeypatch, fault
):
    monkeypatch.setenv("AGENT6_OUTCOME_SERVICE_TOKEN", KEY)
    body = event().model_dump(mode="json")
    headers = {"Authorization": "Bearer " + KEY}
    if fault == "credential":
        headers = {}
    elif fault == "publisher":
        body["source_id"] = "unknown"
    elif fault == "unverified":
        body["verification_status"] = "PENDING"
    elif fault == "nonfinal":
        body["status"] = "PAYMENT_SUCCEEDED"
    else:
        body["occurred_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with TestClient(app) as client:
        response = client.post("/business/events", json=body, headers=headers)
    assert response.status_code == (401 if fault == "credential" else 422)
    assert not confirmations(isolate_prediction_log)


def test_api_records_actual_probability_and_preserves_existing_response(
    isolate_prediction_log, monkeypatch
):
    from API import services

    class Native:
        best_threshold = 0.3

        def predict_proba(self, frame):
            return [0.4]

    monkeypatch.setattr(services.model, "predict", lambda frame: [1])
    monkeypatch.setattr(services.model, "unwrap_python_model", lambda: Native())
    with TestClient(app) as client:
        response = client.post(
            "/predict", json=FEATURES, headers={"X-Transaction-ID": "payment-42"}
        )
    assert response.json() == {"prediction": 1}
    assert float(response.headers["X-Fraud-Probability"]) == 0.4
    assert response.headers["X-Transaction-ID"] == "payment-42"
    with sqlite3.connect(isolate_prediction_log.database) as db:
        import json

        record = json.loads(db.execute("SELECT payload FROM predictions").fetchone()[0])
    assert record["positive_probability"] == 0.4 and record["decision_threshold"] == 0.3
    assert record["input_ref"].startswith("sha256:")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.2, 1.2])
def test_invalid_probability_is_not_durably_recorded(isolate_prediction_log, score):
    with pytest.raises(ValueError):
        isolate_prediction_log.append(1, datetime.now(UTC), positive_probability=score)
    with sqlite3.connect(isolate_prediction_log.database) as db:
        assert db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
