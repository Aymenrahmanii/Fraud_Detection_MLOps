"""Provider-neutral storage interfaces for trusted application wiring."""

from datetime import datetime
from typing import Protocol

from agent6_mlops_lifecycle.contracts.mlops_projects import EvaluationBatch, ProjectManifest
from agent6_mlops_lifecycle.contracts.mlops_storage import StoredEvaluationReference


class ProjectManifestRepository(Protocol):
    def register_manifest(self, tenant_id: str, manifest: ProjectManifest) -> None: ...

    def get_manifest(
        self, tenant_id: str, project_id: str, model_id: str, model_version: str
    ) -> ProjectManifest | None: ...


class EvaluationBatchRepository(Protocol):
    def save_batch(
        self,
        tenant_id: str,
        manifest: ProjectManifest,
        batch: EvaluationBatch,
        window_start: datetime,
        window_end: datetime,
    ) -> StoredEvaluationReference: ...

    def load_batch(self, reference: StoredEvaluationReference) -> EvaluationBatch: ...


class ProjectStorageError(RuntimeError):
    """A storage failure prevents trusted evaluation."""


class ProjectStorageConflictError(ProjectStorageError):
    """An immutable project registration was reused with different settings."""


class EvidenceNotFoundError(ProjectStorageError):
    """Evidence is missing or outside the requested scope."""


class EvidenceIntegrityError(ProjectStorageError):
    """Stored evidence or metadata failed verification."""


class ProjectStorageUnavailableError(ProjectStorageError):
    """The configured database cannot complete the operation."""
