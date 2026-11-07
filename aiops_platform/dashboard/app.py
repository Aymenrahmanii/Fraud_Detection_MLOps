"""Operator Dashboard (v5 section 7): incidents, audit trail, approvals.

The Dashboard never calls Kubernetes or an agent. A human decision is sent
to the Orchestrator (`OrchestratorPort.resolve_approval`), which validates it
through the HITL Service and resumes the workflow.

Operators authenticate with a bearer token mapped to a subject and roles in
`operators.yaml`; roles come from that server-side mapping, never from the
request. (A production deployment puts SSO/OIDC in front of this.)
"""

import hmac
from pathlib import Path
from typing import Any, Protocol

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import generate_latest
from pydantic import BaseModel, Field

from aiops_platform.bus.base import TransientError
from aiops_platform.context.service import ContextService, Principal
from aiops_platform.hitl.service import HitlError, Operator

WEB = Path(__file__).with_name("web")
DEFAULT_OPERATORS = Path(__file__).with_name("operators.example.yaml")


class OrchestratorPort(Protocol):
    async def resolve_approval(
        self, approval_id: str, *, decision: str, operator: Operator, comment: str
    ) -> dict[str, Any]: ...
    async def status(self, incident_id: str) -> dict[str, Any]: ...
    async def start_release(self, **kwargs: Any) -> dict[str, Any]: ...
    async def start_model_check(self, **kwargs: Any) -> dict[str, Any]: ...


class Resolution(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    comment: str = Field(min_length=3, max_length=2000)


class ReleaseRequest(BaseModel):
    service_id: str
    namespace: str
    current_version: str
    version: str
    commit: str
    environment: str = "production"
    assessment: dict[str, Any] | None = None


def load_operators(path: Path) -> dict[str, Operator]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        entry["token"]: Operator(subject=entry["subject"], roles=frozenset(entry.get("roles", [])))
        for entry in data.get("operators", [])
    }


def _clean_approval(row: dict[str, Any]) -> dict[str, Any]:
    request = dict(row["request_json"])
    request.pop("_provenance", None)
    plan = request.get("plan") or {}
    if plan:
        # The approver sees the exact change; the opaque operation blob stays server-side.
        request["plan"] = {
            k: plan.get(k) for k in ("status", "reason", "operation_id", "plan_hash", "patch_hash", "target", "patch")
        }
    return {
        "approval_id": row["approval_id"],
        "incident_id": row["incident_id"],
        "action_id": row["action_id"],
        "state": row["state"],
        "decision": row.get("decision"),
        "approver_id": row.get("approver_id"),
        "reason": row.get("reason"),
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "request": request,
    }


def create_app(
    *,
    context: ContextService,
    orchestrator: OrchestratorPort,
    operators: dict[str, Operator] | None = None,
    metrics_registry=None,
    scenarios: dict[str, Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="AIOps Platform Dashboard", version="1.0")
    tokens = operators if operators is not None else load_operators(DEFAULT_OPERATORS)
    viewer = Principal.dashboard()

    def operator(authorization: str = Header(default="")) -> Operator:
        token = authorization.removeprefix("Bearer ").strip()
        for known, op in tokens.items():
            if token and hmac.compare_digest(known, token):
                return op
        raise HTTPException(401, "unknown operator token")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/api/me")
    async def me(op: Operator = Depends(operator)) -> dict[str, Any]:
        return {"subject": op.subject, "roles": sorted(op.roles)}

    @app.get("/api/incidents")
    async def incidents(_: Operator = Depends(operator)) -> list[dict[str, Any]]:
        rows = await context.list_incidents(viewer)
        return [
            {
                "incident_id": c.incident_id,
                "kind": c.kind,
                "status": c.status,
                "priority": c.priority,
                "severity": c.severity,
                "environment": c.environment,
                "service": c.service.model_dump() if c.service else None,
                "opened_at": c.opened_at.isoformat() if c.opened_at else None,
                "closed_at": c.closed_at.isoformat() if c.closed_at else None,
                "hitl": c.hitl.model_dump(),
            }
            for c in rows
        ]

    @app.get("/api/incidents/{incident_id}")
    async def incident(incident_id: str, _: Operator = Depends(operator)) -> dict[str, Any]:
        try:
            ctx = await context.dashboard_incident(viewer, incident_id)
        except KeyError as exc:
            raise HTTPException(404, "incident not found") from exc
        workflow = await orchestrator.status(incident_id)
        trail = await context.audit_trail(viewer, incident_id)
        return {
            "context": ctx.model_dump(mode="json"),
            "workflow": {
                k: workflow.get(k)
                for k in (
                    "stage",
                    "kind",
                    "outcome",
                    "selected",
                    "policy",
                    "consensus",
                    "notes",
                    "replans",
                    "approval",
                    "waiting",
                    "done",
                )
            },
            "audit": [
                {
                    "seq": a["seq"],
                    "at": a["created_at"].isoformat(),
                    "actor": a["actor"],
                    "action": a["action"],
                    "detail": a["detail_json"],
                }
                for a in trail
            ],
            "audit_chain_valid": await context.verify_audit_chain(viewer, incident_id),
        }

    @app.get("/api/approvals")
    async def approvals(
        state: str | None = "WAITING_FOR_HUMAN", _: Operator = Depends(operator)
    ) -> list[dict[str, Any]]:
        return [_clean_approval(r) for r in await context.list_approvals(viewer, state=state or None)]

    @app.post("/api/approvals/{approval_id}/resolve")
    async def resolve(approval_id: str, body: Resolution, op: Operator = Depends(operator)) -> dict[str, Any]:
        try:
            return await orchestrator.resolve_approval(
                approval_id, decision=body.decision, operator=op, comment=body.comment
            )
        except HitlError as exc:
            raise HTTPException(409 if exc.code != "APPROVER_NOT_AUTHORIZED" else 403, f"{exc.code}: {exc}") from exc
        except TransientError as exc:
            raise HTTPException(
                503, "The platform is briefly unavailable; nothing was approved or executed. Retry in a few seconds."
            ) from exc

    @app.post("/api/releases")
    async def release(body: ReleaseRequest, op: Operator = Depends(operator)) -> dict[str, Any]:
        if not op.roles & {"release-manager", "admin"}:
            raise HTTPException(403, "release-manager role required")
        release_id = f"REL-{body.service_id}-{body.version}"
        return await orchestrator.start_release(
            release_id=release_id,
            service={"service_id": body.service_id, "namespace": body.namespace, "version": body.current_version},
            environment=body.environment,
            version=body.version,
            commit=body.commit,
            assessment=body.assessment,
        )

    @app.post("/api/model-checks/{model_name}")
    async def model_check(model_name: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        if not op.roles & {"ml-lead", "admin"}:
            raise HTTPException(403, "ml-lead role required")
        from datetime import UTC, datetime

        check_id = f"MLOPS-{model_name}-{datetime.now(UTC):%Y%m%d%H%M%S}"
        return await orchestrator.start_model_check(
            check_id=check_id,
            service={"service_id": model_name, "namespace": "ml", "version": "current"},
            environment="production",
        )

    if scenarios:

        @app.get("/api/scenarios")
        async def list_scenarios(_: Operator = Depends(operator)) -> list[str]:
            return sorted(scenarios)

        @app.post("/api/scenarios/{name}")
        async def run_scenario(name: str, op: Operator = Depends(operator)) -> dict[str, Any]:
            if "admin" not in op.roles and "sre" not in op.roles:
                raise HTTPException(403, "sre role required to inject faults")
            if name not in scenarios:
                raise HTTPException(404, "unknown scenario")
            return {"opened": await scenarios[name]()}

    if metrics_registry is not None:

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> PlainTextResponse:
            return PlainTextResponse(generate_latest(metrics_registry).decode(), media_type="text/plain; version=0.0.4")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
