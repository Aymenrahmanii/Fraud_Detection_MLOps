"""Fraud inference API with durable, scoped prediction evidence for Agent 6."""

import hmac
import logging
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from prometheus_fastapi_instrumentator import Instrumentator

from API.schemas import FraudRequest, FraudResponse
from API.services import MODEL_ARTIFACT_SHA256, predict_fraud_with_evidence
from API.telemetry.business_events import BusinessEvent, TRANSACTION_ID, receive_event
from API.telemetry.business import (
    BusinessOutcome,
    BusinessOutcomeConflict,
    resolve_outcome,
)
from API.telemetry.config import TelemetryConfig
from API.telemetry.operations import record_failure
from API.telemetry.store import PredictionLogError, SQLitePredictionLog

logger = logging.getLogger(__name__)
app = FastAPI(
    title="Fraud Detection API",
    description="Predict fraud and record bounded model-evaluation evidence",
    version="1.0.0",
)
Instrumentator().instrument(app).expose(app)


@lru_cache(maxsize=1)
def get_prediction_log() -> SQLitePredictionLog:
    try:
        settings = TelemetryConfig.from_env()
        return SQLitePredictionLog(
            settings.database, settings.scope, MODEL_ARTIFACT_SHA256
        )
    except (PredictionLogError, OSError, ValueError) as error:
        logger.error("Prediction log initialization failed (%s)", type(error).__name__)
        raise HTTPException(
            status_code=503, detail="Prediction recording is unavailable"
        ) from None


@app.exception_handler(RequestValidationError)
async def invalid_prediction(request: Request, error: RequestValidationError):
    if request.url.path == "/predict" and request.method == "POST":
        try:
            provider = app.dependency_overrides.get(
                get_prediction_log, get_prediction_log
            )
            record_failure(
                provider(),
                error.body if isinstance(error.body, dict) else {},
                0.0,
                status_code=422,
            )
        except (PredictionLogError, OSError, ValueError, HTTPException):
            logger.error("Validation telemetry recording is unavailable")
    return await request_validation_exception_handler(request, error)


@app.get("/health")
def health_check(
    prediction_log: Annotated[SQLitePredictionLog, Depends(get_prediction_log)],
) -> dict[str, str]:
    return {"status": "ok"}


@app.post("/predict", response_model=FraudResponse)
def predict(
    request: FraudRequest,
    response: Response,
    http_request: Request,
    prediction_log: Annotated[SQLitePredictionLog, Depends(get_prediction_log)],
) -> FraudResponse:
    observed_at = datetime.now(UTC)
    transaction_id = http_request.headers.get("X-Transaction-ID", str(uuid4()))
    if transaction_id is not None:
        try:
            transaction_id = TRANSACTION_ID.validate_python(transaction_id)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Invalid business transaction ID"
            ) from None
    started = time.perf_counter()
    try:
        prediction, probability, threshold = predict_fraud_with_evidence(
            request.model_dump()
        )
    except Exception:
        try:
            record_failure(
                prediction_log,
                request.model_dump(),
                (time.perf_counter() - started) * 1000,
            )
        except (PredictionLogError, OSError, ValueError):
            logger.error("Failed inference telemetry could not be recorded")
        raise
    try:
        event = prediction_log.append(
            prediction,
            observed_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            inputs=request.model_dump(),
            positive_probability=probability,
            decision_threshold=threshold,
            transaction_id=transaction_id,
        )
    except BusinessOutcomeConflict:
        raise HTTPException(
            status_code=409, detail="Conflicting business transaction binding"
        ) from None
    except (PredictionLogError, OSError, ValueError) as error:
        logger.error("Prediction recording failed (%s)", type(error).__name__)
        raise HTTPException(
            status_code=503, detail="Prediction recording is unavailable"
        ) from None
    response.headers["X-Observation-ID"] = event.observation_id
    response.headers["X-Model-Version"] = event.model_version
    if transaction_id is not None:
        response.headers["X-Transaction-ID"] = transaction_id
    if probability is not None:
        response.headers["X-Fraud-Probability"] = str(probability)
        response.headers["X-Decision-Threshold"] = str(threshold)
    return FraudResponse(prediction=prediction)


@app.post("/business/events")
def receive_business_event(
    body: BusinessEvent,
    request: Request,
    prediction_log: Annotated[SQLitePredictionLog, Depends(get_prediction_log)],
) -> dict[str, object]:
    config = TelemetryConfig.from_env()
    if len(config.outcome_token) < 32 or not hmac.compare_digest(
        request.headers.get("Authorization", ""), "Bearer " + config.outcome_token
    ):
        raise HTTPException(
            status_code=401, detail="A trusted business-service credential is required"
        )
    try:
        with prediction_log._connect() as connection:
            return receive_event(
                connection, config.scope, body, config.business_source_ids
            )
    except BusinessOutcomeConflict:
        raise HTTPException(
            status_code=409, detail="Conflicting business confirmation rejected"
        ) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except PredictionLogError:
        raise HTTPException(
            status_code=503, detail="Business event storage is unavailable"
        ) from None


@app.post("/business/outcomes")
def receive_business_outcome(
    body: BusinessOutcome,
    request: Request,
    prediction_log: Annotated[SQLitePredictionLog, Depends(get_prediction_log)],
) -> dict[str, object]:
    config = TelemetryConfig.from_env()
    expected = config.outcome_token
    provided = request.headers.get("Authorization", "")
    if len(expected) < 32 or not hmac.compare_digest(provided, "Bearer " + expected):
        raise HTTPException(
            status_code=401, detail="A trusted business-service credential is required"
        )
    try:
        with prediction_log._connect() as connection:
            sequence = resolve_outcome(connection, config.scope, body)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except BusinessOutcomeConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except PredictionLogError:
        raise HTTPException(
            status_code=503, detail="Business outcome storage is unavailable"
        ) from None
    return {
        "status": "RECORDED",
        "event_id": str(body.event_id),
        "sequence_id": sequence,
    }
