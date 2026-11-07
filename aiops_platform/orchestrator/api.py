"""Orchestrator HTTP API used by the Dashboard in the distributed deployment.

Only the Dashboard service calls it (service token). It forwards the operator
the Dashboard authenticated; the HITL Service then checks that operator's
roles against the action class.
"""

from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest
from pydantic import BaseModel

from aiops_platform.context.service import Principal, Role
from aiops_platform.hitl.service import HitlError, Operator
from aiops_platform.rpc.identity import H_PRINCIPAL, H_TOKEN, Credential, Unauthenticated, Verifier


class OperatorIn(BaseModel):
    subject: str
    roles: list[str]


class ResolveIn(BaseModel):
    decision: str
    comment: str
    operator: OperatorIn


def create_orchestrator_api(orchestrator, verifier: Verifier) -> FastAPI:
    app = FastAPI(title="Orchestrator API")

    def dashboard(x_aiops_principal: str = Header(default=""), x_aiops_token: str = Header(default="")) -> Principal:
        try:
            principal = verifier.principal(((H_PRINCIPAL, x_aiops_principal), (H_TOKEN, x_aiops_token)))
        except Unauthenticated as exc:
            raise HTTPException(401, str(exc)) from exc
        if principal.role is not Role.DASHBOARD:
            raise HTTPException(403, "only the Dashboard calls the Orchestrator API")
        return principal

    @app.get("/workflows/{incident_id}")
    async def status(incident_id: str, _: Principal = Depends(dashboard)) -> dict[str, Any]:
        return await orchestrator.status(incident_id)

    @app.post("/approvals/{approval_id}/resolve")
    async def resolve(approval_id: str, body: ResolveIn, _: Principal = Depends(dashboard)) -> dict[str, Any]:
        try:
            return await orchestrator.resolve_approval(
                approval_id,
                decision=body.decision,
                operator=Operator(subject=body.operator.subject, roles=frozenset(body.operator.roles)),
                comment=body.comment,
            )
        except HitlError as exc:
            raise HTTPException(403 if exc.code == "APPROVER_NOT_AUTHORIZED" else 409, f"{exc.code}: {exc}") from exc

    @app.post("/releases")
    async def release(body: dict[str, Any], _: Principal = Depends(dashboard)) -> dict[str, Any]:
        return await orchestrator.start_release(**body)

    @app.post("/model-checks")
    async def model_check(body: dict[str, Any], _: Principal = Depends(dashboard)) -> dict[str, Any]:
        return await orchestrator.start_model_check(**body)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            generate_latest(orchestrator.metrics.registry).decode(), media_type="text/plain; version=0.0.4"
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


class HttpOrchestrator:
    """The Dashboard's `OrchestratorPort` over the Orchestrator API."""

    def __init__(self, base_url: str, credential: Credential) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30, headers=dict(credential.metadata()))

    async def _json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in (403, 409):
            code, _, message = response.json().get("detail", "").partition(": ")
            error = HitlError(message or code)
            error.code = code
            raise error
        return response.raise_for_status().json()

    async def status(self, incident_id: str) -> dict[str, Any]:
        return await self._json(await self._http.get(f"/workflows/{incident_id}"))

    async def resolve_approval(
        self, approval_id: str, *, decision: str, operator: Operator, comment: str
    ) -> dict[str, Any]:
        body = {
            "decision": decision,
            "comment": comment,
            "operator": {"subject": operator.subject, "roles": sorted(operator.roles)},
        }
        return await self._json(await self._http.post(f"/approvals/{approval_id}/resolve", json=body))

    async def start_release(self, **kwargs: Any) -> dict[str, Any]:
        return await self._json(await self._http.post("/releases", json=kwargs))

    async def start_model_check(self, **kwargs: Any) -> dict[str, Any]:
        return await self._json(await self._http.post("/model-checks", json=kwargs))
