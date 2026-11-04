"""Authenticated lifecycle state, exact-candidate HITL and deployment-agent handoff."""

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.lifecycle.service import LifecycleService


class ReviewDecision(Contract):
    decision: Literal["APPROVE", "REJECT"]
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(min_length=3, max_length=1000)


class DeploymentResult(Contract):
    action: Literal["DEPLOY", "ROLLBACK"] = "DEPLOY"
    status: Literal["SUCCEEDED", "FAILED"]
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_version: str = Field(min_length=1, max_length=160)
    evidence_ref: str = Field(min_length=1, max_length=160)


def install_routes(
    app: FastAPI, service: LifecycleService, authorize: Callable[..., Awaitable[str]]
) -> None:
    bearer = HTTPBearer(auto_error=False)

    async def machine(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        expected = service.policy.integration_token.get_secret_value()
        if (
            len(expected) < 32
            or credentials is None
            or not hmac.compare_digest(credentials.credentials, expected)
        ):
            raise HTTPException(
                status_code=401, detail="A deployment-integration credential is required"
            )
        return "deployment-agent"

    @app.get("/api/lifecycle")
    async def lifecycle(actor: Annotated[str, Depends(authorize)]) -> dict[str, Any]:
        return await asyncio.to_thread(service.snapshot)

    @app.post("/api/lifecycle/{identity}/review")
    async def review(
        identity: str, body: ReviewDecision, actor: Annotated[str, Depends(authorize)]
    ) -> dict[str, Any]:
        try:
            job = await asyncio.to_thread(service.store.evidence.job, identity)
            if job is None:
                raise LookupError("Review request not found")
            if body.decision == "APPROVE":
                await asyncio.to_thread(service.validate_candidate, job)
            job = await asyncio.to_thread(
                service.store.resolve,
                identity,
                body.decision,
                body.candidate_sha256,
                actor,
                body.note,
            )
            if job["status"] == "DEPLOYMENT_READY":
                await asyncio.to_thread(service.handoff, job)
            return job
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.get("/api/integrations/deployments")
    async def deployments(actor: Annotated[str, Depends(machine)]) -> dict[str, Any]:
        result = []
        for job in await asyncio.to_thread(service.store.jobs):
            if job["status"] == "DEPLOYMENT_READY":
                reference = await asyncio.to_thread(service.handoff, job)
                result.append(
                    await asyncio.to_thread(
                        service.store.evidence.get, "deployment-request", reference
                    )
                )
            elif job["status"] == "ROLLBACK_REQUESTED":
                result.append(
                    await asyncio.to_thread(
                        service.store.evidence.get, "rollback-request", job["rollback_ref"]
                    )
                )
        return {"requests": result}

    @app.post("/api/integrations/deployments/{identity}/result")
    async def deployment_result(
        identity: str, body: DeploymentResult, actor: Annotated[str, Depends(machine)]
    ) -> dict[str, Any]:
        job = await asyncio.to_thread(service.store.evidence.job, identity)
        if job is None:
            raise HTTPException(status_code=404, detail="Deployment request not found")
        if body.candidate_sha256 != job.get("candidate_sha256"):
            raise HTTPException(
                status_code=409, detail="Deployment result differs from the exact candidate"
            )
        expected = "DEPLOYMENT_READY" if body.action == "DEPLOY" else "ROLLBACK_REQUESTED"
        if job["status"] != expected:
            previous = job.get("deployment_result")
            if previous == body.model_dump():
                return job
            raise HTTPException(
                status_code=409, detail="Deployment result conflicts with the lifecycle state"
            )
        if body.action == "DEPLOY" and datetime.now(UTC) > datetime.fromisoformat(
            job["review_expires_at"]
        ):
            raise HTTPException(status_code=409, detail="Deployment authorization expired")
        wanted_version = (
            "artifact-" + body.candidate_sha256 if body.action == "DEPLOY" else job["model_version"]
        )
        if body.status == "SUCCEEDED" and body.model_version != wanted_version:
            raise HTTPException(
                status_code=409, detail="Acknowledgment identifies another model version"
            )
        if body.action == "DEPLOY" and body.status == "SUCCEEDED":
            try:
                await asyncio.to_thread(service.validate_candidate, job)
            except (ValueError, OSError) as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
        audit = {
            "job_id": identity,
            "actor": actor,
            "at": datetime.now(UTC).isoformat(),
            **body.model_dump(),
        }
        reference = await asyncio.to_thread(service.store.evidence.put, "deployment-result", audit)
        state = "DEPLOYED" if body.action == "DEPLOY" else "ROLLED_BACK"
        if body.status == "FAILED":
            state = "DEPLOYMENT_FAILED" if body.action == "DEPLOY" else "ROLLBACK_FAILED"
        if body.action == "DEPLOY" and body.status == "FAILED":
            rollback = {
                "schema_version": "1",
                "kind": "MODEL_ROLLBACK_REQUEST",
                "request_id": "rollback-" + identity,
                "original_job_id": identity,
                "current_model_version": "artifact-" + body.candidate_sha256,
                "restore_model_version": job["model_version"],
                "evidence_ref": body.evidence_ref,
                "destination": "deployment-agent",
                "scope": service.config.scope().model_dump(),
            }
            rollback_ref = await asyncio.to_thread(
                service.store.evidence.put, "rollback-request", rollback
            )
            changes = {"status": "ROLLBACK_REQUESTED", "rollback_ref": rollback_ref}
        else:
            changes = {"status": state}
        try:
            return await asyncio.to_thread(
                service.store.save,
                job,
                **changes,
                deployment_result=body.model_dump(),
                deployment_result_ref=reference,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
