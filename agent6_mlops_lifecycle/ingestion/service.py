"""Compose a trusted source, registered policy and immutable batch repository."""

from agent6_mlops_lifecycle.contracts.mlops_ingestion import (
    ObservationIngestionReceipt,
    ObservationIngestionRequest,
)
from agent6_mlops_lifecycle.ingestion.builder import build_observation_window
from agent6_mlops_lifecycle.interfaces.observations import ObservationSource
from agent6_mlops_lifecycle.interfaces.project_storage import (
    EvaluationBatchRepository,
    ProjectManifestRepository,
)


class ObservationIngestionService:
    def __init__(
        self,
        *,
        manifests: ProjectManifestRepository,
        batches: EvaluationBatchRepository,
        tenant_id: str,
    ) -> None:
        if not tenant_id.strip() or len(tenant_id) > 160:
            raise ValueError("Tenant identity must be nonempty and bounded")
        self._manifests = manifests
        self._batches = batches
        self.tenant_id = tenant_id

    def ingest(
        self, query: ObservationIngestionRequest, source: ObservationSource
    ) -> ObservationIngestionReceipt:
        query = ObservationIngestionRequest.model_validate_json(query.model_dump_json())
        if query.tenant_id != self.tenant_id:
            raise ValueError("Ingestion tenant differs from configured service tenant")
        manifest = self._manifests.get_manifest(
            self.tenant_id, query.project_id, query.model_id, query.model_version
        )
        if manifest is None:
            raise ValueError("Register the exact project and model version before ingestion")
        built = build_observation_window(query, manifest, source.read_snapshot())
        reference = (
            self._batches.save_batch(
                self.tenant_id, manifest, built.batch, query.window_start, query.window_end
            )
            if built.batch is not None
            else None
        )
        return ObservationIngestionReceipt(
            query=query,
            summary=built.summary,
            status="STORED" if reference is not None else "INSUFFICIENT_EVIDENCE",
            reference=reference,
        )
