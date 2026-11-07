"""Injected authorization boundary.

Agent 4 validates authorization; it never issues approvals or tokens. The
production-safe default denies every live mutation.
"""

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from agent4_deployment_recovery.core.errors import AuthorizationDeniedError
from agent4_deployment_recovery.core.schemas import (
    MAX_CLOCK_SKEW,
    AuthorizedOperation,
    Identifier,
    NonEmptyStr,
    StrictModel,
)


class AuthorizationDecision(StrictModel):
    authorized: bool
    verifier_id: Identifier
    checked_at: datetime
    reason: NonEmptyStr
    authorization_reference: str | None = Field(
        default=None, min_length=3, max_length=256
    )

    @model_validator(mode="after")
    def validate_decision(self):
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        if self.checked_at > datetime.now(UTC) + MAX_CLOCK_SKEW:
            raise ValueError("checked_at is in the future")
        if self.authorized and self.authorization_reference is None:
            raise ValueError("an authorization reference is required for approval")
        return self


@runtime_checkable
class AuthorizationVerifier(Protocol):
    """Trust boundary supplied by an external authorization system."""

    def verify(self, operation: AuthorizedOperation) -> AuthorizationDecision:
        """Validate the operation's credential and exact approval scope."""


class DenyAllAuthorizationVerifier:
    """Safe default used whenever no trusted verifier has been configured."""

    verifier_id = "deny-all-unconfigured"

    def verify(self, operation: AuthorizedOperation) -> AuthorizationDecision:
        del operation
        return AuthorizationDecision(
            authorized=False,
            verifier_id=self.verifier_id,
            checked_at=datetime.now(UTC),
            reason="no trusted authorization verifier is configured",
        )


def require_authorized(
    operation: AuthorizedOperation,
    verifier: AuthorizationVerifier | None = None,
    *,
    at: datetime | None = None,
) -> AuthorizationDecision:
    """Return a positive decision or fail closed."""
    current = at or datetime.now(UTC)
    try:
        operation.ensure_fresh(at=current)
    except ValueError as exc:
        raise AuthorizationDeniedError(str(exc)) from exc

    selected = verifier if verifier is not None else DenyAllAuthorizationVerifier()
    decision = selected.verify(operation)
    if not isinstance(decision, AuthorizationDecision):
        raise AuthorizationDeniedError(
            "authorization verifier returned an invalid decision"
        )
    if not decision.authorized:
        raise AuthorizationDeniedError(decision.reason)
    checked_at = decision.checked_at.astimezone(UTC)
    current_utc = current.astimezone(UTC)
    if not current_utc - MAX_CLOCK_SKEW <= checked_at <= current_utc + MAX_CLOCK_SKEW:
        raise AuthorizationDeniedError("authorization decision is stale")
    return decision
