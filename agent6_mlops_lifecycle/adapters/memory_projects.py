"""Compatibility project registry for development-only prepared batches."""

from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectManifest


class MemoryProjectManifestRepository:
    def __init__(self) -> None:
        self._manifests: dict[tuple[str, str, str, str], ProjectManifest] = {}

    def register_manifest(self, tenant_id: str, manifest: ProjectManifest) -> None:
        value = ProjectManifest.model_validate_json(manifest.model_dump_json())
        key = (tenant_id, value.project_id, value.model_id, value.model_version)
        previous = self._manifests.get(key)
        if previous is not None and previous != value:
            raise ValueError("A registered model version cannot silently change policy")
        self._manifests[key] = value

    def get_manifest(
        self, tenant_id: str, project_id: str, model_id: str, model_version: str
    ) -> ProjectManifest | None:
        manifest = self._manifests.get((tenant_id, project_id, model_id, model_version))
        return ProjectManifest.model_validate_json(manifest.model_dump_json()) if manifest else None
