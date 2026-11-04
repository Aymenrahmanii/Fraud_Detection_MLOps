"""Connect the existing fraud artifacts and historical holdout to the generic scheduler."""

import ast
import os
from pathlib import Path

from agent6_mlops_lifecycle.assurance.catalog import import_fraud_holdout
from agent6_mlops_lifecycle.assurance.models import BenchmarkRegistry, ModelArtifact
from agent6_mlops_lifecycle.assurance.store import AssuranceStore
from agent6_mlops_lifecycle.monitoring.models import MonitorScope


def configure_fraud_benchmark(
    project: Path,
    database: Path,
    registry_path: Path,
    scope: MonitorScope,
    *,
    include_streamlit: bool,
) -> Path:
    """Import labels once at startup; no prediction or outcome log is touched."""
    store = AssuranceStore(database, scope)
    dataset_id = import_fraud_holdout(project / "Data/combined_holdout.csv", store)
    # Read the application's literal fallback without importing/loading the serving model.
    tree = ast.parse((project / "API/services.py").read_text(encoding="utf-8"))
    default = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "MODEL_ARTIFACT_PATH"
                for target in node.targets
            )
            and (
                isinstance(node.value, ast.Call)
                and len(node.value.args) == 2
                and isinstance(node.value.args[1], ast.Constant)
            )
        ):
            default = node.value.args[1].value
    configured = os.environ.get("MODEL_ARTIFACT_PATH", default)
    if not isinstance(configured, str):
        raise ValueError("Cannot determine the configured fraud API artifact")
    api = Path(configured)
    if not api.is_absolute():
        api = project / api
    models = [ModelArtifact(name="Fraud API", kind="mlflow", path=api.resolve(strict=True))]
    if include_streamlit:
        models.append(
            ModelArtifact(
                name="Fraud Streamlit",
                kind="joblib",
                path=(project / "Notebooks/artifacts/fraud_pipeline.pkl").resolve(strict=True),
            )
        )
    registry = BenchmarkRegistry(
        project_root=project.resolve(strict=True), dataset_id=dataset_id, models=tuple(models)
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    return registry_path
