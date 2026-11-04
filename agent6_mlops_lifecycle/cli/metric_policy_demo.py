"""Evaluate independent project policies with multiple metric gates."""

import argparse
import asyncio
import json
from pathlib import Path

from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.mlops_projects import EvaluationBatch, ProjectManifest
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService

ROOT = Path(__file__).resolve().parents[1]


async def run(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    database = output / "projects.sqlite3"
    store = SQLiteProjectStore(database)
    service = ProjectEvaluationService(manifests=store, batches=store, tenant_id="demo")
    results: list[dict[str, object]] = []
    for name in ("support-routing", "energy-demand"):
        base = ROOT / "contracts/examples/agent6/metrics"
        manifest = ProjectManifest.model_validate_json(
            (base / f"{name}-manifest.json").read_bytes()
        )
        batch = EvaluationBatch.model_validate_json((base / f"{name}-batch.json").read_bytes())
        service.register_project(manifest)
        if batch.evidence_context is None:
            raise RuntimeError("Metrics demo requires a declared evidence context")
        reference = service.store_batch(
            batch, batch.evidence_context.window_start, batch.evidence_context.window_end
        )
        (output / f"{name}-reference.json").write_text(
            reference.model_dump_json(indent=2), encoding="utf-8"
        )
        reopened = SQLiteProjectStore(database)
        evaluator = ProjectEvaluationService(manifests=reopened, batches=reopened, tenant_id="demo")
        report = await evaluator.evaluate_reference(reference)
        (output / f"{name}-report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        results.append(
            {
                "project_id": report.project_id,
                "decision": report.decision,
                "outcome_coverage": report.outcome_coverage,
                "gates": [
                    {
                        "metric_id": result.gate.metric_id,
                        "value": result.value,
                        "operator": result.gate.operator,
                        "threshold": result.gate.threshold,
                        "status": result.status,
                    }
                    for result in report.gate_results
                ],
                "evidence_ref": report.evidence_ref,
            }
        )
    return {
        "database": str(database.resolve()),
        "projects": results,
        "limitations": [
            "Synthetic fixtures demonstrate configurable gates, not production quality.",
            "Thresholds are illustrative and require project-owner calibration.",
            "No LLM, live connector, training or deployment is invoked.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "artifacts/agent6-metric-policy-demo"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output_dir))
        serialized = json.dumps(report, indent=2, allow_nan=False)
        (args.output_dir / "metric-policy-demo-report.json").write_text(
            serialized, encoding="utf-8"
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(serialized)


if __name__ == "__main__":
    main()
