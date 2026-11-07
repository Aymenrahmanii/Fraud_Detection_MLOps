"""Transactions persist automatically; only authenticated verified events resolve them."""

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from API.main import app

SERVICE = "test-business-source-credential-not-a-real-secret"
PAYLOAD = {
    "Category": "shopping",
    "paymentMethod": "paypal",
    "isWeekend": 0,
    "numItems": 2,
    "localTime": 4.7,
    "paymentMethodAgeDays": 0,
    "accountAgeDays": 30,
}


def predict_and_outcome(client):
    response = client.post("/predict", json=PAYLOAD)
    assert response.status_code == 200
    return {
        "event_id": str(uuid4()),
        "observation_id": response.headers["X-Observation-ID"],
        "model_version": response.headers["X-Model-Version"],
        "actual": 1,
        "evidence_ref": "independent-case:verified",
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def test_application_database_initializes_without_seeded_transactions_or_answers(
    isolate_prediction_log,
):
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    with sqlite3.connect(isolate_prediction_log.database) as c:
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM resolved_outcomes").fetchone()[0] == 0


def test_actual_app_request_persists_transaction_and_no_guessed_outcome(
    isolate_prediction_log,
):
    with TestClient(app) as client:
        event = predict_and_outcome(client)
    with sqlite3.connect(isolate_prediction_log.database) as c:
        row = c.execute(
            "SELECT observation_id,inputs_json FROM transactions"
        ).fetchone()
        assert row[0] == event["observation_id"] and "paypal" in row[1]
        assert c.execute("SELECT count(*) FROM resolved_outcomes").fetchone()[0] == 0


def test_verified_business_event_requires_service_auth_and_retries_are_idempotent(
    isolate_prediction_log, monkeypatch
):
    monkeypatch.setenv("AGENT6_OUTCOME_SERVICE_TOKEN", SERVICE)
    with TestClient(app) as client:
        event = predict_and_outcome(client)
        assert client.post("/business/outcomes", json=event).status_code == 401
        headers = {"Authorization": "Bearer " + SERVICE}
        result = client.post("/business/outcomes", json=event, headers=headers)
        assert result.status_code == 200
        assert (
            client.post("/business/outcomes", json=event, headers=headers).json()
            == result.json()
        )
        assert (
            client.post(
                "/business/outcomes", json={**event, "actual": 0}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/business/outcomes",
                json={**event, "observation_id": str(uuid4())},
                headers=headers,
            ).status_code
            == 404
        )
    with sqlite3.connect(isolate_prediction_log.database) as c:
        assert (
            c.execute("SELECT count(*) FROM agent6_business_outcomes_v1").fetchone()[0]
            == 1
        )
        assert c.execute("SELECT actual FROM resolved_outcomes").fetchone()[0] == 1


def test_unknown_or_future_business_events_never_create_outcomes(
    isolate_prediction_log, monkeypatch
):
    monkeypatch.setenv("AGENT6_OUTCOME_SERVICE_TOKEN", SERVICE)
    with TestClient(app) as client:
        event = predict_and_outcome(client)
        result = client.post(
            "/business/outcomes",
            json={**event, "occurred_at": "2099-01-01T00:00:00Z"},
            headers={"Authorization": "Bearer " + SERVICE},
        )
        assert result.status_code == 422
    with sqlite3.connect(isolate_prediction_log.database) as c:
        assert c.execute("SELECT count(*) FROM resolved_outcomes").fetchone()[0] == 0
