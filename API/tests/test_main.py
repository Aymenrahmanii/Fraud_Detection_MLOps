"""API contract tests.

These exercise the FastAPI layer (routing, request validation, response
shape) against a mocked model (see conftest.py) -- they are not a
substitute for actually verifying the real model loads and predicts
correctly, which requires the MLflow registry portability bug to be
fixed first.
"""

from fastapi.testclient import TestClient

import API.services as services
from API.main import app

client = TestClient(app)

VALID_PAYLOAD = {
    "paymentMethod": "paypal",
    "numItems": 4,
    "localTime": 4.742303,
    "paymentMethodAgeDays": 0,
    "accountAgeDays": 1,
}


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_predict_happy_path_returns_model_prediction(monkeypatch):
    monkeypatch.setattr(services.model, "predict", lambda df: [1])

    response = client.post("/predict", json=VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"prediction": 1}


def test_predict_passes_request_fields_through_to_model(monkeypatch):
    captured = {}

    def fake_predict(df):
        captured["columns"] = sorted(df.columns.tolist())
        captured["payment_method"] = df.iloc[0]["paymentMethod"]
        return [0]

    monkeypatch.setattr(services.model, "predict", fake_predict)

    client.post("/predict", json=VALID_PAYLOAD)

    assert captured["payment_method"] == "paypal"
    assert "numItems" in captured["columns"]


def test_predict_missing_required_field_returns_422():
    payload = dict(VALID_PAYLOAD)
    del payload["paymentMethod"]  # required, no default

    response = client.post("/predict", json=payload)

    assert response.status_code == 422


def test_predict_wrong_type_returns_422():
    payload = dict(VALID_PAYLOAD)
    payload["numItems"] = "not-a-number"

    response = client.post("/predict", json=payload)

    assert response.status_code == 422
