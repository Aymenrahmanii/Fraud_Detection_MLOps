"""Run an explicit historical-data diagnostic; never claim verified production outcomes."""

import asyncio
import json
from datetime import UTC, datetime

from agent6_mlops_lifecycle.assurance.bootstrap import configure_fraud_benchmark
from agent6_mlops_lifecycle.lifecycle.service import LifecycleService
from agent6_mlops_lifecycle.monitoring.settings import ROOT, MonitoringSettings
from agent6_mlops_lifecycle.monitoring.state import MonitorState


def main() -> None:
    data = ROOT / "artifacts/agent6/diagnostics" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    config = MonitoringSettings(
        source_database=data / "application.sqlite3",
        state_database=data / "state.sqlite3",
        evidence_database=data / "evidence.sqlite3",
        benchmark_database=data / "assurance.sqlite3",
        tenant_id="diagnostic",
        project_id="fraud-agent6-diagnostic",
        advisor_enabled=False,
    )
    registry = configure_fraud_benchmark(
        ROOT,
        config.benchmark_database,
        data / "benchmark-registry.json",
        config.scope(),
        include_streamlit=True,
    )
    config = config.model_copy(update={"benchmark_registry": registry})
    service = LifecycleService(config, MonitorState(config.state_database, config.scope()))
    job = service.store.schedule(
        "diagnostic-historical-only",
        "diagnostic-" + data.name,
        service.policy.model_dump(mode="json"),
    )
    if job is None:
        raise RuntimeError("Diagnostic was not scheduled")
    asyncio.run(service.train(job, []))
    result = service.store.evidence.job(job["job_id"])
    print(
        json.dumps(
            {
                "historical_diagnostic_only": True,
                "verified_production_guard": False,
                "result": result,
                "data_directory": str(data),
            },
            indent=2,
        )
    )
    if result is None or result["status"] == "FAILED":
        raise SystemExit(1)
