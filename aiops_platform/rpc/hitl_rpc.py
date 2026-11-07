"""HITL Service and Monitoring Service over gRPC.

* HITL: the Orchestrator creates/resolves approvals; Agent 4's provenance
  verifier (synchronous, in Agent 4's worker thread) reads the record it must
  trust through `SyncHitlProvenance`.
* Monitoring: Agent 4's bound recovery request -> Agent 1's signed verdict,
  served by the Agent 1 worker; Agent 4 calls it through `SyncVerdictClient`.
"""

import json
import logging
from typing import Any

import grpc

from agent4_deployment_recovery.core.integration_models import (
    RecoveryVerificationRequestV1,
    SignedRecoveryVerificationEnvelopeV1,
)
from aiops_platform.bus.base import TransientError
from aiops_platform.context.service import AccessDenied, Principal, Role
from aiops_platform.contracts.enums import AgentId
from aiops_platform.hitl.service import ApprovalRequest, ApprovalState, HitlError, HitlService, Operator
from aiops_platform.rpc.codec import dumps, loads, revive_datetimes
from aiops_platform.rpc.gen import platform_pb2 as pb
from aiops_platform.rpc.gen import platform_pb2_grpc as rpc
from aiops_platform.rpc.identity import Credential, Unauthenticated, Verifier

log = logging.getLogger(__name__)
DEADLINE_SECONDS = 15.0
AGENT4 = Principal.agent(AgentId.DEPLOYMENT_RECOVERY)


async def _abort(context, exc: BaseException):
    if isinstance(exc, Unauthenticated):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
    if isinstance(exc, AccessDenied):
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
    if isinstance(exc, HitlError):
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, f"{exc.code}: {exc}")
    if isinstance(exc, TransientError):
        # A dependency (Context Service) is down: tell the caller to retry, not that we broke.
        log.warning("rpc unavailable: %s", exc)
        await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
    log.exception("rpc failed")
    await context.abort(grpc.StatusCode.INTERNAL, type(exc).__name__)


class HitlServicer(rpc.HITLServiceServicer):
    def __init__(self, hitl: HitlService, verifier: Verifier) -> None:
        self._hitl = hitl
        self._verifier = verifier

    async def _run(self, request, context, fn):
        try:
            principal = self._verifier.principal(context.invocation_metadata())
            return pb.Reply(result_json=dumps(await fn(principal, loads(request.args_json) or {})))
        except Exception as exc:  # noqa: BLE001
            await _abort(context, exc)

    async def CreateApproval(self, request, context):
        return await self._run(
            request, context, lambda p, a: self._hitl.create_approval(p, ApprovalRequest.model_validate(a["request"]))
        )

    async def IssuePolicyPermit(self, request, context):
        return await self._run(
            request,
            context,
            lambda p, a: self._hitl.issue_policy_permit(p, ApprovalRequest.model_validate(a["request"])),
        )

    async def ResolveApproval(self, request, context):
        return await self._run(
            request,
            context,
            lambda p, a: self._hitl.resolve_approval(
                p,
                a["approval_id"],
                decision=a["decision"],
                operator=Operator(subject=a["operator"]["subject"], roles=frozenset(a["operator"]["roles"])),
                comment=a["comment"],
            ),
        )

    async def GetApprovalState(self, request, context):
        return await self._run(request, context, lambda p, a: self._hitl.get_approval_state(p, a["approval_id"]))

    async def ExpireDue(self, request, context):
        async def expire(principal, _args):
            if principal.role is not Role.ORCHESTRATOR:
                raise AccessDenied("only the Orchestrator sweeps approvals")
            return await self._hitl.expire_due()

        return await self._run(request, context, expire)

    async def ProvenanceRecord(self, request, context):
        async def record(principal, args):
            if principal not in (AGENT4, Principal.orchestrator()):
                raise AccessDenied("only Agent 4 verifies approval provenance")
            return await self._hitl.provenance_record(args["approval_id"])

        return await self._run(request, context, record)


class GrpcHitlClient:
    """Orchestrator-side client with `HitlClient`'s API."""

    def __init__(self, channel: grpc.aio.Channel, credential: Credential) -> None:
        self._stub = rpc.HITLServiceStub(channel)
        self._credential = credential

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        try:
            reply = await getattr(self._stub, name)(
                pb.Call(args_json=dumps(args)), metadata=self._credential.metadata(), timeout=DEADLINE_SECONDS
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() is grpc.StatusCode.FAILED_PRECONDITION:
                code, _, message = (exc.details() or "").partition(": ")
                error = HitlError(message)
                error.code = code
                raise error from exc
            if exc.code() is grpc.StatusCode.PERMISSION_DENIED:
                raise AccessDenied(exc.details()) from exc
            if exc.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                raise TransientError(f"HITL Service {exc.code().name}: {exc.details()}") from exc
            raise
        return loads(reply.result_json)

    async def create_approval(self, request: ApprovalRequest) -> ApprovalState:
        return ApprovalState.model_validate(await self._call("CreateApproval", {"request": request}))

    async def issue_policy_permit(self, request: ApprovalRequest) -> ApprovalState:
        return ApprovalState.model_validate(await self._call("IssuePolicyPermit", {"request": request}))

    async def resolve_approval(
        self, approval_id: str, *, decision: str, operator: Operator, comment: str
    ) -> ApprovalState:
        return ApprovalState.model_validate(
            await self._call(
                "ResolveApproval",
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "operator": {"subject": operator.subject, "roles": sorted(operator.roles)},
                    "comment": comment,
                },
            )
        )

    async def get_approval_state(self, approval_id: str) -> ApprovalState:
        return ApprovalState.model_validate(await self._call("GetApprovalState", {"approval_id": approval_id}))

    async def expire_due(self) -> list[str]:
        return await self._call("ExpireDue", {})


class SyncHitlProvenance:
    """Agent 4's `approval_lookup` (it runs in a worker thread, so a blocking client)."""

    def __init__(self, target: str, credential: Credential, channel_credentials=None) -> None:
        channel = (
            grpc.secure_channel(target, channel_credentials) if channel_credentials else grpc.insecure_channel(target)
        )
        self._stub = rpc.HITLServiceStub(channel)
        self._credential = credential

    def __call__(self, approval_id: str) -> dict[str, Any] | None:
        reply = self._stub.ProvenanceRecord(
            pb.Call(args_json=json.dumps({"approval_id": approval_id})),
            metadata=self._credential.metadata(),
            timeout=DEADLINE_SECONDS,
        )
        row = loads(reply.result_json)
        return revive_datetimes(row) if row else None


class MonitoringServicer(rpc.MonitoringServiceServicer):
    """Served by the Agent 1 worker."""

    def __init__(self, verdicts, verifier: Verifier, health=None) -> None:
        self._verdicts = verdicts  # Agent1VerdictService
        self._verifier = verifier
        self._health = health

    async def VerifyExecution(self, request, context):
        try:
            principal = self._verifier.principal(context.invocation_metadata())
            if principal != AGENT4:
                raise AccessDenied("only Agent 4 requests bound recovery verdicts")
            args = loads(request.args_json)
            envelope = await self._verdicts.verify_execution(
                RecoveryVerificationRequestV1.model_validate_json(json.dumps(args["request"])), args["service"]
            )
            return pb.Reply(result_json=envelope.model_dump_json())
        except Exception as exc:  # noqa: BLE001
            await _abort(context, exc)

    async def GetServiceHealth(self, request, context):
        try:
            self._verifier.principal(context.invocation_metadata())
            args = loads(request.args_json)
            signals = self._health(args["service_id"], args.get("namespace")) if self._health else {}
            return pb.Reply(result_json=dumps(signals))
        except Exception as exc:  # noqa: BLE001
            await _abort(context, exc)


class SyncVerdictClient:
    """Agent 4's verdict callable: (request, service) -> signed Agent 1 verdict."""

    def __init__(self, target: str, credential: Credential, channel_credentials=None, timeout: float = 600.0) -> None:
        channel = (
            grpc.secure_channel(target, channel_credentials) if channel_credentials else grpc.insecure_channel(target)
        )
        self._stub = rpc.MonitoringServiceStub(channel)
        self._credential = credential
        self._timeout = timeout  # an observation window can last minutes

    def __call__(
        self, request: RecoveryVerificationRequestV1, service: dict[str, str]
    ) -> SignedRecoveryVerificationEnvelopeV1:
        reply = self._stub.VerifyExecution(
            pb.Call(args_json=json.dumps({"request": json.loads(request.model_dump_json()), "service": service})),
            metadata=self._credential.metadata(),
            timeout=self._timeout,
        )
        return SignedRecoveryVerificationEnvelopeV1.model_validate_json(reply.result_json)


def add_hitl_service(server: grpc.aio.Server, hitl: HitlService, verifier: Verifier) -> None:
    rpc.add_HITLServiceServicer_to_server(HitlServicer(hitl, verifier), server)


def add_monitoring_service(server: grpc.aio.Server, verdicts, verifier: Verifier, health=None) -> None:
    rpc.add_MonitoringServiceServicer_to_server(MonitoringServicer(verdicts, verifier, health), server)
