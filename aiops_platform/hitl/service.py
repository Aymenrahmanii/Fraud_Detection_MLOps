"""HITL Service (v5 sections 7 and 8).

The Orchestrator is the only caller. It freezes one exact candidate action
(already prepared and dry-run by Agent 4) and calls `create_approval`; the
Dashboard shows it to a human; the human decision comes back to the
Orchestrator, which calls `resolve_approval`. Only then is a permit signed.

The permit is Agent 4's own `SignedAuthorizationEnvelopeV1`, bound to the
operation, target UID and resourceVersion, plan hash, patch hash, approval
scope and rollback scope, so Agent 4 refuses it for anything else. It carries a
`HUMAN_INTERACTIVE` provenance reference that Agent 4 re-checks against this
service's records (`provenance.py`).
"""

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.integration_models import (
    ApprovalMethod,
    ExternalAuthorizationClaimsV1,
    SignedAuthorizationEnvelopeV1,
)
from aiops_platform.context.service import AccessDenied, ContextService, Principal, Role
from aiops_platform.contracts.platform_payloads import PlanPrepared
from aiops_platform.hitl.keys import AGENT4_AUDIENCE, SigningKey

PROVIDER_ID = "aiops-hitl-service"
PERMIT_TTL = timedelta(minutes=10)

# Which operator roles may approve which action class (v5 section 18 "Approval authorization").
DEFAULT_APPROVER_ROLES: dict[str, frozenset[str]] = {
    "ROLLBACK": frozenset({"sre", "admin"}),
    "SCALE_REPLICAS": frozenset({"sre", "admin"}),
    "INCREASE_MEMORY": frozenset({"sre", "admin"}),
    "DEPLOY": frozenset({"release-manager", "admin"}),
    "PROMOTE_MODEL": frozenset({"ml-lead", "admin"}),
}


class HitlError(Exception):
    code = "HITL_ERROR"


class ApprovalNotFound(HitlError):
    code = "APPROVAL_NOT_FOUND"


class ApprovalNotPending(HitlError):
    code = "APPROVAL_NOT_PENDING"


class ApproverNotAuthorized(HitlError):
    code = "APPROVER_NOT_AUTHORIZED"


class Operator(BaseModel):
    """An authenticated human; built by the Dashboard from its session, never from input."""

    model_config = ConfigDict(frozen=True)
    subject: str = Field(min_length=3)
    roles: frozenset[str]


class ApprovalRequest(BaseModel):
    """What the Orchestrator asks a human to approve (v5 section 8.1 payload)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    approval_id: str
    incident_id: str
    correlation_id: str
    environment: str
    priority: str
    severity: str
    action_id: str
    action_version: int = Field(ge=1)
    action: str
    parameters: dict[str, JsonValue]
    risk: dict[str, JsonValue]
    decision_confidence: float
    reason: str
    evidence_summary: tuple[str, ...] = ()
    hitl_triggers: tuple[str, ...] = ()
    # The exact Agent 4 plan. None for a governance decision (e.g. model
    # promotion review) that no executor acts on: no permit is ever signed then.
    plan: PlanPrepared | None = None
    ttl_seconds: int = Field(default=600, ge=30, le=24 * 3600)


HitlStateName = Literal["WAITING_FOR_HUMAN", "APPROVED", "REJECTED", "EXPIRED"]


class ApprovalState(BaseModel):
    model_config = ConfigDict(frozen=True)
    approval_id: str
    incident_id: str
    state: HitlStateName
    action_id: str
    action_version: int
    expires_at: datetime
    decision: str | None = None
    approver_id: str | None = None
    comment: str | None = None
    # Returned only by the APPROVE resolution; stored as a hash.
    approval_token: str | None = Field(default=None, repr=False)
    # What Agent 4 needs to rebuild its ApprovalReceipt.
    receipt: dict[str, JsonValue] | None = None


def encode_token(envelope: SignedAuthorizationEnvelopeV1) -> str:
    raw = envelope.model_dump_json().encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: str) -> SignedAuthorizationEnvelopeV1:
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    return SignedAuthorizationEnvelopeV1.model_validate_json(raw)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _evidence_hash(request: dict[str, Any], decision: str, approver: str, comment: str, at: datetime) -> str:
    return canonical_sha256(
        {"request": request, "decision": decision, "approver": approver, "comment": comment, "at": at}
    )


@dataclass
class HitlService:
    context: ContextService
    signer: SigningKey
    approver_roles: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(DEFAULT_APPROVER_ROLES))
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _me: Principal = field(default_factory=Principal.hitl)

    # ---- orchestrator calls --------------------------------------------

    async def create_approval(self, principal: Principal, request: ApprovalRequest) -> ApprovalState:
        self._require_orchestrator(principal)
        if request.plan is not None and (
            request.plan.status != "PREPARED" or not request.plan.plan_hash or not request.plan.patch_hash
        ):
            raise HitlError("only an exact, dry-run-validated plan can be submitted for approval")
        existing = await self.context.get_approval(self._me, request.approval_id)
        if existing is not None:  # idempotent: the Orchestrator may retry the call
            return self._state(existing)
        now = self.clock()
        row = {
            "approval_id": request.approval_id,
            "incident_id": request.incident_id,
            "action_id": request.action_id,
            "action_version": request.action_version,
            "state": "WAITING_FOR_HUMAN",
            "decision": None,
            "approver_id": None,
            "reason": None,
            "token_hash": None,
            "request_json": request.model_dump(mode="json"),
            "created_at": now,
            "expires_at": now + timedelta(seconds=request.ttl_seconds),
            "resolved_at": None,
        }
        await self.context.upsert_approval(self._me, row)
        return self._state(row)

    async def resolve_approval(
        self,
        principal: Principal,
        approval_id: str,
        *,
        decision: Literal["APPROVE", "REJECT"],
        operator: Operator,
        comment: str,
    ) -> ApprovalState:
        self._require_orchestrator(principal)
        row = await self._pending(approval_id)
        request = ApprovalRequest.model_validate(row["request_json"])
        allowed = self.approver_roles.get(request.action, frozenset({"admin"}))
        if not operator.roles & allowed:
            raise ApproverNotAuthorized(
                f"{operator.subject} needs one of {sorted(allowed)} to resolve {request.action}"
            )
        now = self.clock()
        row = dict(row)
        row.update(
            decision=decision,
            approver_id=operator.subject,
            reason=comment,
            resolved_at=now,
            state="APPROVED" if decision == "APPROVE" else "REJECTED",
        )
        token = None
        if decision == "APPROVE":
            evidence = _evidence_hash(row["request_json"], decision, operator.subject, comment, now)
            if request.plan is not None:
                token = self._sign_permit(request, operator.subject, ApprovalMethod.HUMAN_INTERACTIVE, evidence, now)
                row["token_hash"] = token_hash(token)
            row["request_json"] = {
                **row["request_json"],
                "_provenance": {
                    "evidence_hash": evidence,
                    "method": ApprovalMethod.HUMAN_INTERACTIVE.value,
                    "approved_at": now.isoformat(),
                },
            }
        await self.context.upsert_approval(self._me, row)
        return self._state(row, token=token)

    async def issue_policy_permit(self, principal: Principal, request: ApprovalRequest) -> ApprovalState:
        """AUTO decision: record it and sign with AUTOMATED_POLICY provenance.

        Agent 4 refuses AUTOMATED_POLICY provenance wherever it requires a signed
        human permit (production); see ARCHITECTURE.md.
        """
        self._require_orchestrator(principal)
        if request.plan is None:
            raise HitlError("a policy permit needs an exact Agent 4 plan")
        now = self.clock()
        subject = "policy:auto"
        evidence = _evidence_hash(request.model_dump(mode="json"), "APPROVE", subject, "AUTO", now)
        token = self._sign_permit(request, subject, ApprovalMethod.AUTOMATED_POLICY, evidence, now)
        row = {
            "approval_id": request.approval_id,
            "incident_id": request.incident_id,
            "action_id": request.action_id,
            "action_version": request.action_version,
            "state": "APPROVED",
            "decision": "APPROVE",
            "approver_id": subject,
            "reason": "approved automatically by policy (AUTO)",
            "token_hash": token_hash(token),
            "request_json": {
                **request.model_dump(mode="json"),
                "_provenance": {
                    "evidence_hash": evidence,
                    "method": ApprovalMethod.AUTOMATED_POLICY.value,
                    "approved_at": now.isoformat(),
                },
            },
            "created_at": now,
            "expires_at": now + timedelta(seconds=request.ttl_seconds),
            "resolved_at": now,
        }
        await self.context.upsert_approval(self._me, row)
        return self._state(row, token=token)

    async def get_approval_state(self, principal: Principal, approval_id: str) -> ApprovalState:
        if principal.role not in (Role.ORCHESTRATOR, Role.DASHBOARD):
            raise AccessDenied(f"{principal.name} may not read approvals")
        row = await self.context.get_approval(self._me, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        return self._state(await self._expire_if_due(row))

    async def expire_due(self) -> list[str]:
        """Mark overdue approvals EXPIRED; returns their IDs (Orchestrator replans them)."""
        expired = []
        for row in await self.context.list_approvals(self._me, state="WAITING_FOR_HUMAN"):
            if (await self._expire_if_due(row))["state"] == "EXPIRED":
                expired.append(row["approval_id"])
        return expired

    # ---- Agent 4 provenance lookups --------------------------------------

    async def provenance_record(self, approval_id: str) -> dict[str, Any] | None:
        return await self.context.get_approval(self._me, approval_id)

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _require_orchestrator(principal: Principal) -> None:
        # v5 section 7: agents never request or resolve approvals; only the Orchestrator.
        if principal.role is not Role.ORCHESTRATOR:
            raise AccessDenied(f"{principal.name} may not create or resolve approvals")

    async def _pending(self, approval_id: str) -> dict[str, Any]:
        row = await self.context.get_approval(self._me, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        row = await self._expire_if_due(row)
        if row["state"] != "WAITING_FOR_HUMAN":
            raise ApprovalNotPending(f"{approval_id} is {row['state']}")
        return row

    async def _expire_if_due(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["state"] == "WAITING_FOR_HUMAN" and row["expires_at"] <= self.clock():
            row = {**row, "state": "EXPIRED", "resolved_at": self.clock()}
            await self.context.upsert_approval(self._me, row)
        return row

    def _sign_permit(
        self,
        request: ApprovalRequest,
        approver: str,
        method: ApprovalMethod,
        evidence_hash: str,
        now: datetime,
    ) -> str:
        operation_expiry = datetime.fromisoformat(str(request.plan.operation["expires_at"]))
        claims = self._claims(request, approver, method, evidence_hash, now, operation_expiry)
        envelope = SignedAuthorizationEnvelopeV1(
            key_id=self.signer.key_id, claims=claims, signature=self.signer.sign(claims)
        )
        return encode_token(envelope)

    def _claims(self, request, approver, method, evidence_hash, now, operation_expiry):
        # Agent 4 models are strict (no type coercion): validate from JSON.
        op = request.plan.operation
        payload = {
            "permit_id": f"PERMIT-{request.approval_id}",
            "issuer": self.signer.issuer,
            "audience": AGENT4_AUDIENCE,
            "issued_at": now.isoformat(),
            "not_before": now.isoformat(),
            "expires_at": min(operation_expiry, now + PERMIT_TTL).isoformat(),
            "operation_id": op["operation_id"],
            "correlation_id": op["correlation_id"],
            "environment": op["environment"],
            "target": op["target"],
            "operation": op["operation"],
            "plan_hash": request.plan.plan_hash,
            "patch_hash": request.plan.patch_hash,
            "authorization_scope_hash": request.plan.authorization_scope_hash,
            "rollback_scope": op["rollback_scope"],
            "approval": {
                "provider_id": PROVIDER_ID,
                "receipt_id": request.approval_id,
                "approver_subject": approver,
                "method": method.value,
                "evidence_hash": evidence_hash,
                "approved_at": now.isoformat(),
            },
        }
        return ExternalAuthorizationClaimsV1.model_validate_json(json.dumps(payload))

    @staticmethod
    def _state(row: Mapping[str, Any], token: str | None = None) -> ApprovalState:
        receipt = None
        if row["state"] == "APPROVED":
            provenance = row["request_json"].get("_provenance", {})
            receipt = {
                "approval_id": row["approval_id"],
                "issuer": PROVIDER_ID,
                "approver_subject": row["approver_id"],
                "approved_at": provenance.get("approved_at"),
                "method": provenance.get("method"),
            }
        return ApprovalState(
            approval_id=row["approval_id"],
            incident_id=row["incident_id"],
            state=row["state"],
            action_id=row["action_id"],
            action_version=row["action_version"],
            expires_at=row["expires_at"],
            decision=row.get("decision"),
            approver_id=row.get("approver_id"),
            comment=row.get("reason"),
            approval_token=token,
            receipt=receipt,
        )
