"""Payloads the shared platform adds to the v5 topic table.

The v5 table (section 3.2) has no topic for the Orchestrator to dispatch a task
to a specialist, and Agent 4 needs a "prepare" step so a human approves the
exact patch before it is signed (see ARCHITECTURE.md). Everything here is
transport payload only; none of it authorizes anything.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from aiops_platform.contracts.base import Contract

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PlanPrepared(Contract):
    """Agent 4's exact, dry-run-validated plan for one candidate action.

    The HITL Service signs a permit over `plan_hash` and `patch_hash`; Agent 4
    re-derives both from a fresh snapshot before mutating and refuses on any
    mismatch, so approving this record approves exactly this patch.
    """

    action_id: Annotated[str, Field(min_length=1)]
    action_version: Annotated[int, Field(ge=1)]
    status: Literal["PREPARED", "REJECTED"]
    reason: str
    operation_id: str | None = None
    plan_hash: Sha256Hex | None = None
    patch_hash: Sha256Hex | None = None
    authorization_scope_hash: Sha256Hex | None = None
    # The exact JSON patch and target, shown to the approver.
    target: dict[str, JsonValue] = Field(default_factory=dict)
    patch: tuple[dict[str, JsonValue], ...] = ()
    # Opaque blob the Agent 4 worker needs to rebuild the operation on execute.
    operation: dict[str, JsonValue] = Field(default_factory=dict)
