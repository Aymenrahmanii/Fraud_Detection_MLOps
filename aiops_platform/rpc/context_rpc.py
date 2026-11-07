"""Context Service over gRPC: server and a client with `ContextService`'s exact API.

The client is a drop-in for the in-process `ContextService`, so
`AgentContext`, `OrchestratorContext`, the HITL Service and the Dashboard work
unchanged. Each client holds one service `Credential`; the server derives the
caller's `Principal` from it and `ContextService` enforces the access rules.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import grpc
from pydantic import ValidationError

from aiops_platform.bus.base import TransientError
from aiops_platform.context.service import AccessDenied, ContextService, Principal
from aiops_platform.context.store import IncidentNotFound
from aiops_platform.contracts.context import CandidateAction, EvidenceReference, IncidentContext
from aiops_platform.contracts.enums import Environment, Priority, Severity
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.contracts.tasks import AgentResult
from aiops_platform.rpc.codec import dumps, loads, revive_datetimes
from aiops_platform.rpc.gen import platform_pb2 as pb
from aiops_platform.rpc.gen import platform_pb2_grpc as rpc
from aiops_platform.rpc.identity import Credential, Unauthenticated, Verifier

log = logging.getLogger(__name__)
DEADLINE_SECONDS = 10.0


def _enum_changes(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    if out.get("severity"):
        out["severity"] = Severity(out["severity"])
    if out.get("priority"):
        out["priority"] = Priority(out["priority"])
    return out


# rpc name -> (ContextService method, decode(args) -> (positional, keyword), encode(result))
SPECS: dict[str, tuple[str, Callable[[dict], tuple[tuple, dict]], Callable[[Any], Any]]] = {
    "OpenIncident": (
        "open_incident",
        lambda a: (
            (),
            {
                "incident_id": a["incident_id"],
                "kind": a["kind"],
                "service": ServiceIdentity(**a["service"]),
                "environment": Environment(a["environment"]),
                "severity": Severity(a["severity"]),
                "priority": Priority(a["priority"]),
                "signals": a.get("signals", {}),
            },
        ),
        lambda r: {"context": r[0], "created": r[1]},
    ),
    "GetIncidentContext": ("get_incident_context", lambda a: ((a["incident_id"],), {}), lambda r: r),
    "GetLatestAgentResults": (
        "get_latest_agent_results",
        lambda a: ((a["incident_id"], tuple(a.get("agent_ids", ()))), {}),
        list,
    ),
    "UpdateIncident": ("update_incident", lambda a: ((a.pop("incident_id"),), _enum_changes(a)), lambda r: r),
    "RecordAction": (
        "record_action",
        lambda a: ((a["incident_id"], CandidateAction.model_validate(a["action"])), {}),
        lambda r: None,
    ),
    "UpdateAction": (
        "update_action",
        lambda a: ((a.pop("incident_id"), a.pop("action_id"), a.pop("action_version")), a),
        lambda r: None,
    ),
    "RecordDecision": ("record_decision", lambda a: ((a.pop("incident_id"),), a), lambda r: r),
    "RecordHITLDecision": ("record_hitl_decision", lambda a: ((a.pop("incident_id"),), a), lambda r: r),
    "RecordModelEvent": ("record_model_event", lambda a: ((a.pop("incident_id"),), a), lambda r: r),
    "AppendAgentResult": (
        "append_agent_result",
        lambda a: ((AgentResult.model_validate(a["result"]), a["result_type"]), {}),
        lambda r: r,
    ),
    "AppendEvidence": (
        "append_evidence",
        lambda a: ((), {**a, "observed_at": datetime.fromisoformat(a["observed_at"])}),
        lambda r: r,
    ),
    "UpsertApproval": ("upsert_approval", lambda a: ((revive_datetimes(a["row"]),), {}), lambda r: None),
    "GetApproval": ("get_approval", lambda a: ((a["approval_id"],), {}), lambda r: r),
    "ReadEvidence": (
        "read_evidence",
        lambda a: ((a["evidence_id"],), {}),
        lambda r: {"reference": r[0], "content": r[1]},
    ),
    "ListIncidents": ("list_incidents", lambda a: ((a.get("limit", 100),), {}), list),
    "DashboardIncident": ("dashboard_incident", lambda a: ((a["incident_id"],), {}), lambda r: r),
    "ListApprovals": ("list_approvals", lambda a: ((), {"state": a.get("state")}), list),
    "AuditTrail": ("audit_trail", lambda a: ((a["incident_id"],), {}), list),
    "VerifyAuditChain": ("verify_audit_chain", lambda a: ((a["incident_id"],), {}), lambda r: r),
}
INCIDENT_REF = {"GetIncidentContext", "DashboardIncident", "AuditTrail", "VerifyAuditChain"}


async def _abort_for(context, exc: BaseException):
    if isinstance(exc, Unauthenticated):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
    if isinstance(exc, AccessDenied):
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
    if isinstance(exc, (IncidentNotFound, KeyError)):
        await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc)[:2000])
    log.exception("context rpc failed")
    await context.abort(grpc.StatusCode.INTERNAL, type(exc).__name__)


def _status_code(exc: BaseException) -> str:
    if isinstance(exc, Unauthenticated):
        return "UNAUTHENTICATED"
    if isinstance(exc, AccessDenied):
        return "PERMISSION_DENIED"
    if isinstance(exc, (IncidentNotFound, KeyError)):
        return "NOT_FOUND"
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return "INVALID_ARGUMENT"
    return "INTERNAL"


class ContextServicer(rpc.ContextServiceServicer):
    def __init__(self, service: ContextService, verifier: Verifier, metrics=None) -> None:
        self._service = service
        self._verifier = verifier
        self._metrics = metrics  # PlatformMetrics: per-method latency and error codes
        # Instance attributes shadow the generated UNIMPLEMENTED base methods.
        for rpc_name in SPECS:
            setattr(self, rpc_name, self._handler(rpc_name))

    def _observe(self, rpc_name: str, code: str, started: float) -> None:
        if self._metrics is not None:
            self._metrics.context_rpc.labels(rpc_name, code).observe(time.perf_counter() - started)

    def _handler(self, rpc_name: str):
        method_name, decode, encode = SPECS[rpc_name]

        async def handle(request, context):
            started = time.perf_counter()
            try:
                principal = self._verifier.principal(context.invocation_metadata())
                args = (
                    {"incident_id": request.incident_id}
                    if rpc_name in INCIDENT_REF
                    else (loads(request.args_json) or {})
                )
                if rpc_name not in INCIDENT_REF and request.incident_id:
                    args.setdefault("incident_id", request.incident_id)
                positional, keyword = decode(args)
                result = await getattr(self._service, method_name)(principal, *positional, **keyword)
                reply = pb.Reply(result_json=dumps(encode(result)))
                self._observe(rpc_name, "OK", started)
                return reply
            except Exception as exc:  # noqa: BLE001 - mapped to a gRPC status
                self._observe(rpc_name, _status_code(exc), started)
                await _abort_for(context, exc)

        return handle


def add_context_service(server: grpc.aio.Server, service: ContextService, verifier: Verifier, metrics=None) -> None:
    rpc.add_ContextServiceServicer_to_server(ContextServicer(service, verifier, metrics), server)


class GrpcContextService:
    """Client with `ContextService`'s API; `principal` arguments must be this credential's."""

    def __init__(self, channel: grpc.aio.Channel, credential: Credential) -> None:
        self._stub = rpc.ContextServiceStub(channel)
        self._credential = credential

    async def _call(self, rpc_name: str, principal: Principal, args: dict[str, Any]) -> Any:
        if principal != self._credential.principal:
            raise AccessDenied(f"this client is {self._credential.principal.name}, not {principal.name}")
        method = getattr(self._stub, rpc_name)
        if rpc_name in INCIDENT_REF:
            request = pb.IncidentRef(incident_id=args["incident_id"])
        else:
            request = pb.Call(incident_id=str(args.get("incident_id") or ""), args_json=dumps(args))
        try:
            reply = await method(request, metadata=self._credential.metadata(), timeout=DEADLINE_SECONDS)
        except grpc.aio.AioRpcError as exc:
            code = exc.code()
            if code is grpc.StatusCode.PERMISSION_DENIED:
                raise AccessDenied(exc.details()) from exc
            if code is grpc.StatusCode.NOT_FOUND:
                raise IncidentNotFound(exc.details()) from exc
            if code in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.RESOURCE_EXHAUSTED,
            ):
                # The Context Service is down or slow: callers retry with backoff.
                raise TransientError(f"Context Service {code.name}: {exc.details()}") from exc
            raise
        return loads(reply.result_json)

    # ---- orchestrator ----
    async def open_incident(self, principal, **kw):
        r = await self._call("OpenIncident", principal, kw)
        return IncidentContext.model_validate(r["context"]), r["created"]

    async def get_incident_context(self, principal, incident_id):
        return IncidentContext.model_validate(
            await self._call("GetIncidentContext", principal, {"incident_id": incident_id})
        )

    async def get_latest_agent_results(self, principal, incident_id, agent_ids=()):
        rows = await self._call(
            "GetLatestAgentResults", principal, {"incident_id": incident_id, "agent_ids": list(agent_ids)}
        )
        return tuple(AgentResult.model_validate(r) for r in rows)

    async def update_incident(self, principal, incident_id, **changes):
        return IncidentContext.model_validate(
            await self._call("UpdateIncident", principal, {"incident_id": incident_id, **changes})
        )

    async def record_action(self, principal, incident_id, action):
        await self._call("RecordAction", principal, {"incident_id": incident_id, "action": action})

    async def update_action(self, principal, incident_id, action_id, action_version, **changes):
        await self._call(
            "UpdateAction",
            principal,
            {"incident_id": incident_id, "action_id": action_id, "action_version": action_version, **changes},
        )

    async def record_decision(self, principal, incident_id, **kw):
        return await self._call("RecordDecision", principal, {"incident_id": incident_id, **kw})

    async def record_hitl_decision(self, principal, incident_id, **kw):
        return await self._call("RecordHITLDecision", principal, {"incident_id": incident_id, **kw})

    async def record_model_event(self, principal, incident_id, **kw):
        return await self._call("RecordModelEvent", principal, {"incident_id": incident_id, **kw})

    async def read_evidence(self, principal, evidence_id):
        r = await self._call("ReadEvidence", principal, {"evidence_id": evidence_id})
        return EvidenceReference.model_validate(r["reference"]), r["content"]

    # ---- agents ----
    async def append_agent_result(self, principal, result, result_type):
        return await self._call(
            "AppendAgentResult",
            principal,
            {"incident_id": result.incident_id, "result": result, "result_type": result_type},
        )

    async def append_evidence(self, principal, **kw):
        return EvidenceReference.model_validate(await self._call("AppendEvidence", principal, kw))

    # ---- HITL ----
    async def upsert_approval(self, principal, row):
        await self._call("UpsertApproval", principal, {"incident_id": row["incident_id"], "row": row})

    async def get_approval(self, principal, approval_id):
        row = await self._call("GetApproval", principal, {"approval_id": approval_id})
        return revive_datetimes(row) if row else None

    # ---- dashboard ----
    async def list_incidents(self, principal, limit=100):
        return [
            IncidentContext.model_validate(r) for r in await self._call("ListIncidents", principal, {"limit": limit})
        ]

    async def dashboard_incident(self, principal, incident_id):
        return IncidentContext.model_validate(
            await self._call("DashboardIncident", principal, {"incident_id": incident_id})
        )

    async def list_approvals(self, principal, state=None):
        return [revive_datetimes(r) for r in await self._call("ListApprovals", principal, {"state": state})]

    async def audit_trail(self, principal, incident_id):
        return [revive_datetimes(r) for r in await self._call("AuditTrail", principal, {"incident_id": incident_id})]

    async def verify_audit_chain(self, principal, incident_id):
        return await self._call("VerifyAuditChain", principal, {"incident_id": incident_id})
