"""Authenticated local monitoring dashboard; operational feedback is independently audited."""

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, JsonValue

from agent6_mlops_lifecycle.assurance.service import AssuranceService
from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_types import Name
from agent6_mlops_lifecycle.lifecycle.api import install_routes
from agent6_mlops_lifecycle.lifecycle.service import LifecycleService
from agent6_mlops_lifecycle.monitoring.business_models import SystemOutcomeEvent
from agent6_mlops_lifecycle.monitoring.engine import MonitoringEngine
from agent6_mlops_lifecycle.monitoring.models import DriftBaseline, OutcomeSubmission
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    OutcomeConflictError,
    OutcomeNotFoundError,
    SourceUnavailableError,
)
from agent6_mlops_lifecycle.monitoring.state import MonitorState

_ASSETS = Path(__file__).resolve().parent / "web"
_bearer = HTTPBearer(auto_error=False)


class PredictionRequest(Contract):
    inputs: dict[str, JsonValue] = Field(min_length=1, max_length=32)


class BaselineApproval(Contract):
    model_version: Name
    note: str = Field(min_length=3, max_length=1000)


def create_app(settings: MonitoringSettings | None = None, *, start_worker: bool = True) -> FastAPI:
    config = settings or MonitoringSettings()
    config.require_token()
    source = DirectObservationSource(config.source_database, config.scope())
    state = MonitorState(config.state_database, config.scope())
    engine = MonitoringEngine(config, source, state)
    assurance = AssuranceService(config, state)
    lifecycle = LifecycleService(config, state)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        task = None
        if start_worker:
            if not await asyncio.to_thread(
                state.acquire,
                engine.owner,
                datetime.now(UTC),
                max(60.0, config.interval_seconds * 5),
            ):
                raise RuntimeError(
                    "Another worker owns this monitor; stop it or use a different state database"
                )
            task = asyncio.create_task(engine.run(), name="agent6-monitoring-worker")
        assurance_task = (
            asyncio.create_task(assurance.run(engine.stopping), name="agent6-assurance-worker")
            if start_worker and assurance.enabled
            else None
        )
        lifecycle_task = (
            asyncio.create_task(lifecycle.run(engine.stopping), name="agent6-lifecycle")
            if start_worker and lifecycle.policy.enabled
            else None
        )
        application.state.worker_task = task
        try:
            yield
        finally:
            if lifecycle_task is not None:
                lifecycle_task.cancel()
                with suppress(asyncio.CancelledError):
                    await lifecycle_task
            if assurance_task is not None:
                assurance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await assurance_task
            if task is not None:
                engine.stop()
                try:
                    await asyncio.wait_for(task, timeout=15)
                except TimeoutError:
                    task.cancel()

    application = FastAPI(
        title="Agent 6 live model monitoring",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.lifecycle = lifecycle
    application.state.assurance = assurance
    application.state.engine = engine
    application.state.source = source
    application.state.monitor_state = state

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> str:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials, config.token.get_secret_value()
        ):
            raise HTTPException(
                status_code=401,
                detail=(
                    "This guided presentation uses a temporary token. Type o and press Enter "
                    "in its running terminal to reopen authenticated access."
                    if config.tenant_id == "verification"
                    else "Use AGENT6_MONITOR_TOKEN from Agent 6 .env; "
                    "Groq API keys do not grant dashboard access."
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )
        return config.reviewer_id

    async def authorize_source(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> str:
        expected = config.outcome_token.get_secret_value()
        if (
            not expected
            or credentials is None
            or not hmac.compare_digest(credentials.credentials, expected)
        ):
            raise HTTPException(
                status_code=401, detail="A trusted outcome-service credential is required"
            )
        return "service:" + config.outcome_source_id

    @application.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @application.get("/")
    async def index() -> FileResponse:
        return FileResponse(_ASSETS / "index.html")

    application.mount("/assets", StaticFiles(directory=_ASSETS), name="monitor-assets")

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        from agent6_mlops_lifecycle.monitoring.metrics import render_metrics

        return Response(
            await asyncio.to_thread(render_metrics, state), media_type="text/plain; version=0.0.4"
        )

    @application.get("/health")
    async def health() -> JSONResponse:
        task = getattr(application.state, "worker_task", None)
        failed = task is not None and task.done()
        return JSONResponse(
            {"status": "worker_failed" if failed else "ok"}, status_code=503 if failed else 200
        )

    @application.post("/api/admin/stop-worker")
    async def stop_worker(actor: Annotated[str, Depends(authorize)]) -> dict[str, str]:
        task = getattr(application.state, "worker_task", None)
        engine.stop()
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=12)
            except TimeoutError:
                raise HTTPException(
                    status_code=503, detail="Worker is finishing its current cycle"
                ) from None
        return {"status": "STOPPED"}

    @application.post("/api/presentation/advisor/refresh")
    async def refresh_presentation_advisor(
        actor: Annotated[str, Depends(authorize)],
    ) -> dict[str, object]:
        if config.tenant_id != "verification" or not config.advisor_enabled:
            raise HTTPException(
                status_code=403,
                detail="Explicit refresh requires an LLM-enabled isolated verification session",
            )
        await assurance.advise(force=True)
        return {"advisor": (await asyncio.to_thread(assurance.snapshot))["advisor"]}

    @application.get("/api/status")
    async def status(actor: Annotated[str, Depends(authorize)]) -> dict[str, object]:
        application_url = None
        if config.application_health_url:
            health_url = urlsplit(config.application_health_url)
            if health_url.hostname in ("127.0.0.1", "localhost", "::1"):
                application_url = urlunsplit((health_url.scheme, health_url.netloc, "/", "", ""))
        heartbeat = await asyncio.to_thread(state.status)
        versions = heartbeat.active_versions if heartbeat else ()
        latest = {}
        for version in versions:
            report = await asyncio.to_thread(state.latest, version)
            if report is not None:
                latest[version] = report.model_dump(mode="json")
        now = datetime.now(UTC)
        stale = heartbeat is None or (now - heartbeat.polled_at).total_seconds() > max(
            10, config.interval_seconds * 3
        )
        task = getattr(application.state, "worker_task", None)
        stale = stale or (task is not None and task.done())
        return {
            "assurance": await asyncio.to_thread(assurance.snapshot),
            "lifecycle": await asyncio.to_thread(lifecycle.snapshot),
            "worker": heartbeat.model_dump(mode="json") if heartbeat else None,
            "stale": stale,
            "reports": list(latest.values()),
            "baselines": {
                version: baseline.model_dump(mode="json")
                for version in versions
                if (baseline := await asyncio.to_thread(state.baseline, version)) is not None
            },
            "alerts": await asyncio.to_thread(state.alerts),
            "operator": actor,
            "configuration": {
                "application_url": application_url,
                "scope": config.scope().model_dump(),
                "baseline_mode": config.baseline_mode,
                "baseline_warmup_seconds": config.baseline_warmup_seconds,
                "quality_window_seconds": config.quality_window_seconds,
                "min_operational_samples": config.min_operational_samples,
                "max_failure_rate": config.max_failure_rate,
                "max_input_issue_rate": config.max_input_issue_rate,
                "max_latency_p95_ms": config.max_latency_p95_ms,
                "manual_review_enabled": config.manual_review_enabled or config.demo_mode,
                "outcome_connector": {
                    "enabled": bool(config.outcome_token.get_secret_value()),
                    "database_configured": bool(config.business_database_url.get_secret_value()),
                    "source_id": config.outcome_source_id,
                    "received_system_outcomes": sum(
                        report.get("system_outcomes", 0) for report in latest.values()
                    ),
                },
                "demo_mode": config.demo_mode,
                "interval_seconds": config.interval_seconds,
                "window_seconds": config.window_seconds,
                "min_samples": config.min_samples,
                "min_quality_score": config.min_quality_score,
                "min_outcome_coverage": config.min_outcome_coverage,
                "min_distribution_samples": config.min_distribution_samples,
                "psi_threshold": config.psi_threshold,
            },
        }

    @application.get("/api/predictions")
    async def predictions(
        actor: Annotated[str, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        model_version: Annotated[str | None, Query(max_length=160)] = None,
    ) -> list[dict[str, object]]:
        start, end = engine.window()
        try:
            frames = await asyncio.to_thread(source.read_frames, start, end, end)
        except SourceUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        records: list[dict[str, object]] = []
        for frame in frames:
            if model_version is not None and frame.model_version != model_version:
                continue
            outcomes = {row.observation_id: row for row in frame.snapshot.outcomes}
            for record in frame.snapshot.predictions:
                outcome = outcomes.get(record.observation_id)
                records.append(
                    {
                        "observation_id": record.observation_id,
                        "observed_at": record.observed_at.isoformat(),
                        "predicted": record.predicted,
                        "positive_probability": record.positive_probability,
                        "decision_threshold": record.decision_threshold,
                        "model_version": record.model_version,
                        "actual": outcome.actual if outcome else None,
                        "outcome_kind": frame.kinds.get(record.observation_id),
                        "confirmed_at": outcome.confirmed_at.isoformat() if outcome else None,
                    }
                )
        return sorted(records, key=lambda row: str(row["observed_at"]), reverse=True)[:limit]

    @application.post("/api/predict")
    async def predict(
        body: PredictionRequest, actor: Annotated[str, Depends(authorize)]
    ) -> dict[str, object]:
        if len(json.dumps(body.inputs, allow_nan=False).encode()) > 16384:
            raise HTTPException(
                status_code=413, detail="Prediction request exceeds the local size limit"
            )
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.post(config.prediction_url, json=body.inputs)
            if response.status_code != 200:
                raise HTTPException(
                    status_code=422, detail="Fraud API rejected the input; check feature types"
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid prediction response")
            identity = str(UUID(response.headers.get("X-Observation-ID", "")))
            event = await asyncio.to_thread(source.get_prediction, identity)
            if (
                event.model_version != response.headers.get("X-Model-Version")
                or type(payload.get("prediction")) is not int
                or event.predicted != payload["prediction"]
            ):
                raise ValueError("API response and recorded evidence differ")
        except (httpx.HTTPError, ValueError, SourceUnavailableError) as error:
            raise HTTPException(
                status_code=503,
                detail="Prediction API or its configured observation log is unavailable",
            ) from error
        engine.wake.set()
        return {
            "observation_id": identity,
            "prediction": event.predicted,
            "model_version": event.model_version,
        }

    @application.post("/api/integrations/outcomes")
    async def system_outcome(
        body: SystemOutcomeEvent, actor: Annotated[str, Depends(authorize_source)]
    ) -> dict[str, object]:
        try:
            prediction = await asyncio.to_thread(source.get_prediction, str(body.observation_id))
            if prediction.model_version != body.model_version:
                raise OutcomeNotFoundError("Outcome does not match the exact deployed model")
            stored = await asyncio.to_thread(
                source.submit_outcome,
                str(body.observation_id),
                OutcomeSubmission(
                    actual=body.actual,
                    note="Trusted business outcome: " + body.evidence_ref,
                    verified=True,
                    kind="SYSTEM",
                    event_id=str(body.event_id),
                    evidence_ref=body.evidence_ref,
                    occurred_at=body.occurred_at,
                ),
                actor,
            )
        except OutcomeNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        except OutcomeConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except SourceUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        engine.wake.set()
        return {
            "status": "ACCEPTED",
            "observation_id": str(body.observation_id),
            "event_id": str(body.event_id),
            "confirmed_at": stored.event.confirmed_at.isoformat(),
        }

    @application.post("/api/outcomes/{observation_id}")
    async def outcome(
        observation_id: UUID, body: OutcomeSubmission, actor: Annotated[str, Depends(authorize)]
    ) -> dict[str, object]:
        if body.kind == "SYSTEM":
            raise HTTPException(
                status_code=403, detail="System outcomes require the integration endpoint"
            )
        if not config.manual_review_enabled and not config.demo_mode:
            raise HTTPException(
                status_code=403,
                detail="Manual review is disabled; use the trusted system connector",
            )
        if body.kind == "SIMULATED" and not config.demo_mode:
            raise HTTPException(
                status_code=403, detail="Scripted outcomes are allowed only in isolated demo mode"
            )
        try:
            stored = await asyncio.to_thread(
                source.submit_outcome, str(observation_id), body, actor
            )
        except OutcomeNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        except OutcomeConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except SourceUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        engine.wake.set()
        return stored.model_dump(mode="json")

    @application.post("/api/baselines")
    async def baseline(
        body: BaselineApproval, actor: Annotated[str, Depends(authorize)]
    ) -> dict[str, object]:
        start, end = engine.window()
        frames = await asyncio.to_thread(source.read_frames, start, end, end)
        frame = next((row for row in frames if row.model_version == body.model_version), None)
        if frame is None:
            raise HTTPException(
                status_code=404, detail="No current predictions for this model version"
            )
        counts = (
            sum(row.predicted == 0 for row in frame.snapshot.predictions),
            sum(row.predicted == 1 for row in frame.snapshot.predictions),
        )
        if sum(counts) < config.min_distribution_samples:
            raise HTTPException(
                status_code=422,
                detail="Reference window has too few predictions for the drift policy",
            )
        approved = DriftBaseline(
            scope=config.scope(),
            model_version=body.model_version,
            prediction_counts=counts,
            prediction_source_ref=frame.snapshot.prediction_source_ref,
            window_start=start,
            window_end=end,
            approved_at=datetime.now(UTC),
            approved_by=actor,
            note=body.note,
        )
        try:
            result = await asyncio.to_thread(state.approve_baseline, approved)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        engine.wake.set()
        return result.model_dump(mode="json")

    install_routes(application, lifecycle, authorize)
    return application
