"""Register projects, save scoped evidence, or evaluate a persisted reference."""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.mlops_projects import EvaluationBatch, ProjectManifest
from agent6_mlops_lifecycle.contracts.mlops_storage import StoredEvaluationReference
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="Register an immutable project policy")
    register.add_argument("--manifest", type=Path, required=True)
    ingest = commands.add_parser("ingest", help="Persist a batch and print its verified reference")
    ingest.add_argument("--batch", type=Path, required=True)
    ingest.add_argument("--window-start", type=datetime.fromisoformat, required=True)
    ingest.add_argument("--window-end", type=datetime.fromisoformat, required=True)
    evaluate = commands.add_parser(
        "evaluate", help="Evaluate saved evidence without re-registering"
    )
    evaluate.add_argument("--reference", type=Path, required=True)
    for command in (register, ingest, evaluate):
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--tenant", required=True, help="Trusted local application scope")
    args = parser.parse_args()
    try:
        store = SQLiteProjectStore(args.database)
        service = ProjectEvaluationService(manifests=store, batches=store, tenant_id=args.tenant)
        if args.command == "register":
            manifest = ProjectManifest.model_validate_json(
                args.manifest.read_text(encoding="utf-8-sig")
            )
            service.register_project(manifest)
            print(manifest.model_dump_json(indent=2))
        elif args.command == "ingest":
            batch = EvaluationBatch.model_validate_json(args.batch.read_text(encoding="utf-8-sig"))
            reference = service.store_batch(batch, args.window_start, args.window_end)
            print(reference.model_dump_json(indent=2))
        else:
            reference = StoredEvaluationReference.model_validate_json(
                args.reference.read_text(encoding="utf-8-sig")
            )
            report = asyncio.run(service.evaluate_reference(reference))
            print(report.model_dump_json(indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
