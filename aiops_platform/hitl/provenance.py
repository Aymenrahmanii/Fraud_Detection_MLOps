"""Approval provenance for Agent 4 (its `ApprovalProvenanceVerifier` port).

Agent 4 does not trust a permit just because the signature is valid: it asks
an independent verifier whether the referenced approval really was a human
decision for exactly this scope. This verifier answers from the HITL
Service's own records.

Agent 4's coordinator is synchronous and runs in a worker thread, so the
record lookup is an injected synchronous callable (a gRPC call in compose, a
bridge to the event loop in-process).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent4_deployment_recovery.core.canonical import canonical_json
from agent4_deployment_recovery.core.integration_models import (
    ApprovalMethod,
    ApprovalProvenanceDecision,
    ApprovalProvenanceReference,
    AuthorizationBinding,
)
from aiops_platform.hitl.service import PROVIDER_ID

VERIFIER_ID = "aiops-hitl-provenance-v1"

ApprovalLookup = Callable[[str], dict[str, Any] | None]


def _same(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


class HitlProvenanceVerifier:
    def __init__(self, lookup: ApprovalLookup, *, require_human: bool = True) -> None:
        self._lookup = lookup
        self._require_human = require_human

    def verify(
        self, reference: ApprovalProvenanceReference, *, binding: AuthorizationBinding
    ) -> ApprovalProvenanceDecision:
        reason = self._check(reference, binding)
        trusted = reason is None
        return ApprovalProvenanceDecision(
            trusted=trusted,
            verifier_id=VERIFIER_ID,
            checked_at=datetime.now(UTC),
            reference=f"{PROVIDER_ID}:{reference.receipt_id}" if trusted else None,
            reason=reason or "human approval verified against the HITL record",
        )

    def _check(self, reference: ApprovalProvenanceReference, binding: AuthorizationBinding) -> str | None:
        if reference.provider_id != PROVIDER_ID:
            return f"unknown approval provider {reference.provider_id}"
        if self._require_human and reference.method != ApprovalMethod.HUMAN_INTERACTIVE:
            return "approval was not made by a human"
        row = self._lookup(reference.receipt_id)
        if row is None:
            return "approval record not found"
        if row["state"] != "APPROVED" or row.get("decision") != "APPROVE":
            return f"approval is {row['state']}, not APPROVED"
        if row.get("approver_id") != reference.approver_subject:
            return "approver does not match the recorded approver"
        provenance = row["request_json"].get("_provenance", {})
        if provenance.get("evidence_hash") != reference.evidence_hash:
            return "approval evidence hash does not match the record"
        if provenance.get("method") != reference.method.value:
            return "approval method does not match the record"
        plan = row["request_json"].get("plan")
        if not plan:
            return "approval was a governance decision, not an execution approval"
        op = plan["operation"]
        if (op["operation_id"], op["correlation_id"], op["environment"]) != (
            binding.operation_id,
            binding.correlation_id,
            binding.environment,
        ):
            return "approval was granted for a different operation"
        if (plan["plan_hash"], plan["patch_hash"], plan["authorization_scope_hash"]) != (
            binding.plan_hash,
            binding.patch_hash,
            binding.authorization_scope_hash,
        ):
            return "approval was granted for a different plan or patch"
        if not _same(op["target"], binding.target.model_dump(mode="json")):
            return "approval was granted for a different target"
        return None
