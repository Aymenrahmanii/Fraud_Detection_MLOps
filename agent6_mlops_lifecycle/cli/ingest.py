"""Match local JSONL predictions and outcomes and persist reproducible evidence."""

import argparse
import asyncio
import json
from pathlib import Path

from agent6_mlops_lifecycle.adapters.jsonl_observations import JSONLObservationSource
from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.mlops_ingestion import (
    LocalObservationSourceConfig,
    ObservationIngestionReceipt,
)
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService
from agent6_mlops_lifecycle.ingestion.service import ObservationIngestionService


def ingest_sources(
    config_path: Path, database: Path, tenant_id: str
) -> ObservationIngestionReceipt:
    config = LocalObservationSourceConfig.model_validate_json(config_path.read_bytes())
    base = config_path.resolve().parent
    source = JSONLObservationSource(base / config.predictions_file, base / config.outcomes_file)
    store = SQLiteProjectStore(database)
    service = ObservationIngestionService(manifests=store, batches=store, tenant_id=tenant_id)
    return service.ingest(config.query, source)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--tenant", required=True, help="Trusted local application scope")
    parser.add_argument("--evaluate", action="store_true", help="Also evaluate the saved reference")
    args = parser.parse_args()
    try:
        receipt = ingest_sources(args.sources, args.database, args.tenant)
        output: dict[str, object] = {"ingestion": receipt.model_dump(mode="json")}
        if args.evaluate and receipt.reference is not None:
            store = SQLiteProjectStore(args.database)
            service = ProjectEvaluationService(
                manifests=store, batches=store, tenant_id=args.tenant
            )
            report = asyncio.run(service.evaluate_reference(receipt.reference))
            output["evaluation"] = report.model_dump(mode="json")
        else:
            output["evaluation"] = None
        print(json.dumps(output, indent=2, allow_nan=False))
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
