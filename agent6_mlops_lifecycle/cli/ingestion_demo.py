"""Replay two project windows before and after delayed outcomes become available."""

import argparse
import asyncio
import json
from pathlib import Path

from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.cli.ingest import ingest_sources
from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectManifest
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService

ROOT = Path(__file__).resolve().parents[1]


async def run(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    database = output / "projects.sqlite3"
    store = SQLiteProjectStore(database)
    results: list[dict[str, object]] = []
    for name in ("support-routing", "energy-demand"):
        manifest = ProjectManifest.model_validate_json(
            (ROOT / f"contracts/examples/agent6/projects/{name}-manifest.json").read_bytes()
        )
        store.register_manifest("demo", manifest)
        for phase in ("early", "late"):
            receipt = ingest_sources(
                ROOT / f"contracts/examples/agent6/ingestion/{name}-sources-{phase}.json",
                database,
                "demo",
            )
            (output / f"{name}-{phase}-receipt.json").write_text(
                receipt.model_dump_json(indent=2), encoding="utf-8"
            )
            if receipt.reference is None:
                raise RuntimeError("The demo expects some confirmed outcomes in both phases")
            (output / f"{name}-{phase}-reference.json").write_text(
                receipt.reference.model_dump_json(indent=2), encoding="utf-8"
            )
            # Read from a fresh service using only the stored evidence reference.
            reopened = SQLiteProjectStore(database)
            evaluator = ProjectEvaluationService(
                manifests=reopened, batches=reopened, tenant_id="demo"
            )
            report = await evaluator.evaluate_reference(receipt.reference)
            (output / f"{name}-{phase}-evaluation.json").write_text(
                report.model_dump_json(indent=2), encoding="utf-8"
            )
            results.append(
                {
                    "project_id": name,
                    "phase": phase,
                    "as_of": receipt.query.as_of.isoformat(),
                    "predictions": receipt.summary.available_prediction_count,
                    "confirmed_outcomes": receipt.summary.matched_count,
                    "pending_outcomes": len(receipt.summary.pending_observation_ids),
                    "outcome_coverage": receipt.summary.outcome_coverage,
                    "duplicate_predictions_ignored": receipt.summary.duplicate_prediction_count,
                    "decision": report.decision,
                    "metrics": report.metrics,
                    "evidence_ref": report.evidence_ref,
                }
            )
    return {
        "database": str(database.resolve()),
        "projects": results,
        "limitations": [
            "Synthetic timestamped files demonstrate replay; no live project is connected.",
            "Only confirmed outcomes available by the cutoff contribute to metrics.",
            "Pending observations remain in the source logs for a later replay.",
            "Local tenant configuration is not production authentication.",
            "Coverage is reported; existing evaluator policy gates apply to matched samples.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/agent6-ingestion-demo")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output_dir))
        serialized = json.dumps(report, indent=2, allow_nan=False)
        (args.output_dir / "ingestion-demo-report.json").write_text(serialized, encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(serialized)


if __name__ == "__main__":
    main()
