"""Domain errors raised by Agent 4's safe core."""


class Agent4Error(Exception):
    """Base class for expected Agent 4 failures."""


class AuthorizationDeniedError(Agent4Error):
    """The configured verifier did not authorize the operation."""


class InvalidTransitionError(Agent4Error):
    """The requested execution state transition is not allowed."""


class JournalConflictError(Agent4Error):
    """A transactional journal precondition was not satisfied."""


class IdempotencyConflictError(JournalConflictError):
    """An idempotency key was reused for a different plan."""


class WorkloadBusyError(JournalConflictError):
    """Another non-terminal execution owns the workload lock."""


class ConcurrentUpdateError(JournalConflictError):
    """An execution record changed since it was read."""


class AuditIntegrityError(Agent4Error):
    """The persisted hash-linked audit chain is invalid."""


class KubernetesAdapterError(Agent4Error):
    """Base class for bounded Kubernetes inspection or dry-run failures."""


class KubernetesConfigurationError(KubernetesAdapterError):
    """An explicit Kubernetes connection selection is missing or invalid."""


class KubernetesNotFoundError(KubernetesAdapterError):
    """The exact namespaced Kubernetes resource was not found."""


class KubernetesForbiddenError(KubernetesAdapterError):
    """The configured identity is not permitted to perform the requested call."""


class KubernetesConflictError(KubernetesAdapterError):
    """A server-enforced resource precondition failed."""


class KubernetesTimeoutError(KubernetesAdapterError):
    """The bounded Kubernetes request timed out."""


class KubernetesConnectivityError(KubernetesAdapterError):
    """The selected Kubernetes API server could not be reached."""


class KubernetesMalformedResponseError(KubernetesAdapterError):
    """Kubernetes returned an incomplete or structurally invalid response."""


class KubernetesUnsupportedError(KubernetesAdapterError):
    """The selected API server does not support the required safe operation."""


class MutationDisabledError(Agent4Error):
    """Live mutation is disabled or lacks a trusted execution permit."""


class UnsafeRollbackError(Agent4Error):
    """Exact rollback preconditions cannot be proven from current state."""


class SignedContractError(Agent4Error):
    """An authenticated cross-process contract failed validation."""


class SignatureVerificationError(SignedContractError):
    """A message signature or trusted issuer binding is invalid."""


class MessageReplayError(SignedContractError):
    """A one-time permit or verdict identifier was replayed."""


class ApprovalProvenanceError(SignedContractError):
    """Trusted HITL or policy approval provenance could not be established."""


class IntegrationContractError(Agent4Error):
    """An agent integration payload cannot be translated safely."""
