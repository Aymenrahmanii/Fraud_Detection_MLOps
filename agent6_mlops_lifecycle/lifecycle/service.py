"Automatic candidate preparation after measured degradation; bounded human review for promotion."

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent6_mlops_lifecycle.assurance.models import BenchmarkDataset, BenchmarkRegistry
from agent6_mlops_lifecycle.assurance.store import AssuranceStore, encode
from agent6_mlops_lifecycle.lifecycle.data import eligible_training_inputs, partition_verified_rows
from agent6_mlops_lifecycle.lifecycle.policy import CandidateReport, promotion_decision
from agent6_mlops_lifecycle.lifecycle.settings import LifecycleSettings
from agent6_mlops_lifecycle.lifecycle.store import LifecycleStore
from agent6_mlops_lifecycle.monitoring.models import MonitoringReport
from agent6_mlops_lifecycle.monitoring.settings import ROOT, MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import DirectObservationSource
from agent6_mlops_lifecycle.monitoring.state import MonitorState
from agent6_mlops_lifecycle.processes import NO_WINDOW_FLAGS


class LifecycleService:
    def __init__(
        self,
        monitor_config: MonitoringSettings,
        monitor: MonitorState,
        policy: LifecycleSettings | None = None,
    ) -> None:
        self.config, self.monitor = monitor_config, monitor
        self.policy = policy or LifecycleSettings()
        if self.policy.allow_verification_jobs and monitor_config.tenant_id != "verification":
            raise ValueError("Verification training opt-in requires an isolated verification scope")
        self.store = LifecycleStore(
            monitor_config.state_database.parent / "lifecycle.sqlite3", monitor_config.scope()
        )
        self.source = DirectObservationSource(
            monitor_config.source_database, monitor_config.scope()
        )
        self.error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        try:
            jobs = self.store.jobs()
            return {
                "enabled": self.policy.enabled,
                "jobs": jobs,
                "error": self.error,
                "automatic_training": self.policy.enabled,
                "automatic_promotion": self.policy.auto_promote,
                "policy_calibrated": self.policy.policy_calibrated,
                "deployment_mode": "DURABLE_HANDOFF",
                "min_verified_labels": self.policy.min_verified_labels,
            }
        except (ValueError, sqlite3.Error, OSError):
            return {
                "enabled": self.policy.enabled,
                "jobs": [],
                "error": "Lifecycle evidence integrity or storage is unavailable",
            }

    def guard(self, report: MonitoringReport) -> list[dict[str, Any]]:
        query = report.ingestion.query
        frames = self.source.read_frames(query.window_start, query.window_end, query.as_of)
        frame = next(row for row in frames if row.model_version == report.model_version)
        predictions = {row.observation_id: row for row in frame.snapshot.predictions}
        result = []
        with self.source._connection() as connection:
            for outcome in frame.snapshot.outcomes:
                if frame.kinds.get(outcome.observation_id) not in ("SYSTEM", "HUMAN"):
                    continue
                row = connection.execute(
                    (
                        "SELECT inputs_json FROM transactions WHERE observation_id=? AND "
                        "tenant_id=? AND project_id=? AND model_id=? AND model_version=?"
                    ),
                    (
                        outcome.observation_id,
                        self.config.tenant_id,
                        self.config.project_id,
                        self.config.model_id,
                        report.model_version,
                    ),
                ).fetchone()
                if row is None or len(row[0]) > 16384:
                    continue
                inputs = json.loads(row[0])
                if not eligible_training_inputs(inputs):
                    continue
                result.append(
                    {
                        "observation_id": outcome.observation_id,
                        "observed_at": predictions[outcome.observation_id].observed_at.isoformat(),
                        "inputs": inputs,
                        "actual": outcome.actual,
                        "predicted": predictions[outcome.observation_id].predicted,
                    }
                )
                if len(result) >= 1000:
                    break
        return result

    async def train(
        self,
        job: dict[str, Any],
        live: list[dict[str, Any]],
        production_fit: list[dict[str, Any]] | None = None,
    ) -> None:
        job = await asyncio.to_thread(self.store.save, job, status="TRAINING")
        process = None
        try:
            if self.config.benchmark_registry is None:
                raise ValueError("Benchmark registry required before candidate training")
            registry = BenchmarkRegistry.model_validate_json(
                self.config.benchmark_registry.read_bytes()
            )
            dataset = BenchmarkDataset.model_validate(
                AssuranceStore(self.config.benchmark_database, self.config.scope()).get(
                    "dataset", registry.dataset_id
                )
            )
            output = ROOT / "artifacts/agent6/candidates" / job["job_id"]
            request = {
                "project_root": str(ROOT),
                "output": str(output),
                "training_data": str(self.policy.training_data),
                "benchmark_rows": [{**row.inputs, "label": row.actual} for row in dataset.rows],
                "benchmark_dataset_id": registry.dataset_id,
                "live_guard": live,
                "production_fit": production_fit or [],
                "guard_origin": "HISTORICAL_ONLY"
                if not live
                else "ISOLATED_VERIFICATION_FIXTURES"
                if self.config.tenant_id == "verification"
                else "VERIFIED_BUSINESS_OUTCOMES",
                "policy": self.policy.model_dump(mode="json"),
                "source_report_id": job["source_report_id"],
            }
            if len(encode(request)) > 2_000_000:
                raise ValueError("Candidate snapshot exceeds its bound")
            snapshot_ref = await asyncio.to_thread(
                self.store.evidence.put, "training-request", request
            )
            job = await asyncio.to_thread(self.store.save, job, input_snapshot_ref=snapshot_ref)
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "SYSTEMROOT",
                    "WINDIR",
                    "PATH",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "LOCALAPPDATA",
                    "APPDATA",
                }
            }
            env.update(
                {
                    "PYTHONPATH": str(ROOT),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "MLFLOW_DISABLE_AGENT_HINT": "1",
                }
            )
            log = output.parent / (job["job_id"] + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("ab") as stream:
                process = await asyncio.create_subprocess_exec(
                    str(self.policy.model_python),
                    "-B",
                    "-m",
                    "agent6_mlops_lifecycle.lifecycle.worker",
                    cwd=ROOT,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stream,
                    creationflags=NO_WINDOW_FLAGS,
                )
                raw, _ = await asyncio.wait_for(
                    process.communicate(encode(request)), timeout=self.policy.timeout_seconds
                )
            if process.returncode != 0 or len(raw) > 1_000_000:
                raise RuntimeError("Candidate worker failed")
            typed_result = CandidateReport.model_validate_json(raw)
            result = typed_result.model_dump(mode="json")
            ref = await asyncio.to_thread(self.store.evidence.put, "candidate-evaluation", result)
            status, reason = promotion_decision(typed_result, self.policy)
            job = await asyncio.to_thread(
                self.store.save,
                job,
                status=status,
                rationale=reason,
                candidate_sha256=result["candidate_sha256"],
                candidate_path=result["candidate_path"],
                evaluation_ref=ref,
                registered_version=result.get("registered_version"),
                guard_origin=result["guard_origin"],
                review_expires_at=(
                    datetime.now(UTC) + timedelta(hours=self.policy.review_expiry_hours)
                ).isoformat(),
            )
            if status == "DEPLOYMENT_READY":
                await asyncio.to_thread(self.handoff, job)
        except (ValueError, RuntimeError, OSError, TimeoutError, KeyError, sqlite3.Error):
            await asyncio.to_thread(
                self.store.save,
                job,
                status="FAILED",
                error="Candidate preparation failed; inspect its owned local worker log",
            )
        finally:
            if process is not None and process.returncode is None:
                if sys.platform == "win32":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=NO_WINDOW_FLAGS,
                    )
                    await killer.wait()
                else:
                    process.kill()
                await process.wait()

    def validate_candidate(self, job: dict[str, Any]) -> None:
        from agent6_mlops_lifecycle.assurance.worker import artifact_digest

        evaluation = CandidateReport.model_validate(
            self.store.evidence.get("candidate-evaluation", job["evaluation_ref"])
        )
        if evaluation.guard_origin == "ISOLATED_VERIFICATION_FIXTURES":
            raise ValueError("Verification fixture candidates cannot authorize deployment")
        if (
            not evaluation.qualified
            or evaluation.candidate_sha256 != job["candidate_sha256"]
            or evaluation.candidate_path != job["candidate_path"]
        ):
            raise ValueError("Candidate identity differs from its qualified evaluation")
        path = Path(job["candidate_path"]).resolve(strict=True)
        if (
            not path.is_relative_to(ROOT / "artifacts/agent6/candidates")
            or artifact_digest(path) != job["candidate_sha256"]
        ):
            raise ValueError("Candidate artifact no longer matches its immutable evaluation")

    def handoff(self, job: dict[str, Any]) -> str:
        self.validate_candidate(job)
        request = {
            "schema_version": "1",
            "kind": "MODEL_DEPLOYMENT_REQUEST",
            "request_id": job["job_id"],
            "candidate_sha256": job["candidate_sha256"],
            "candidate_path": job["candidate_path"],
            "evaluation_ref": job["evaluation_ref"],
            "previous_model_version": job["model_version"],
            "review_ref": job.get("review_ref"),
            "scope": self.config.scope().model_dump(),
            "requires_post_deploy_verification": True,
            "rollback_condition": (
                "Measured regression, serving failure or policy gate failure "
                "on independent confirmed outcomes"
            ),
            "artifact_location": {
                "kind": "shared-agent6-volume",
                "relative_path": Path(job["candidate_path"])
                .relative_to(ROOT / "artifacts/agent6")
                .as_posix(),
            },
            "destination": "deployment-agent",
            "expires_at": job["review_expires_at"],
            "preconditions": {
                "active_model_version": job["model_version"],
                "candidate_sha256": job["candidate_sha256"],
            },
        }
        return self.store.evidence.put("deployment-request", request)

    async def cycle(self) -> None:
        if not self.policy.enabled:
            return
        for job in await asyncio.to_thread(self.store.jobs):
            if job["status"] == "AWAITING_REVIEW" and datetime.now(UTC) > datetime.fromisoformat(
                job["review_expires_at"]
            ):
                await asyncio.to_thread(
                    self.store.save,
                    job,
                    status="EXPIRED",
                    rationale="Candidate review expired; no deployment was authorized",
                )
            if job["status"] == "DEPLOYMENT_READY":
                if datetime.now(UTC) > datetime.fromisoformat(job["review_expires_at"]):
                    await asyncio.to_thread(
                        self.store.save,
                        job,
                        status="EXPIRED",
                        rationale="Deployment handoff expired without confirmation",
                    )
                else:
                    await asyncio.to_thread(self.handoff, job)
            if job["status"] == "DEPLOYED":
                report = await asyncio.to_thread(
                    self.monitor.latest, "artifact-" + job["candidate_sha256"]
                )
                if report and (
                    (report.evaluation and report.evaluation.decision == "FAIL")
                    or (
                        report.operational_health and report.operational_health.status == "DEGRADED"
                    )
                ):
                    rollback = {
                        "schema_version": "1",
                        "kind": "MODEL_ROLLBACK_REQUEST",
                        "request_id": "rollback-" + job["job_id"],
                        "original_job_id": job["job_id"],
                        "current_model_version": "artifact-" + job["candidate_sha256"],
                        "restore_model_version": job["model_version"],
                        "evidence_ref": report.report_id,
                        "destination": "deployment-agent",
                        "scope": self.config.scope().model_dump(),
                    }
                    reference = await asyncio.to_thread(
                        self.store.evidence.put, "rollback-request", rollback
                    )
                    await asyncio.to_thread(
                        self.store.save, job, status="ROLLBACK_REQUESTED", rollback_ref=reference
                    )
            if job["status"] == "TRAINING":
                # A prior process ended before committing its candidate result.
                await asyncio.to_thread(
                    self.store.save,
                    job,
                    status="FAILED",
                    error="Interrupted training; retained for inspection, not silently promoted",
                )
        heartbeat = await asyncio.to_thread(self.monitor.status)
        for version in heartbeat.active_versions if heartbeat else ():
            report = await asyncio.to_thread(self.monitor.latest, version)
            if (
                report is None
                or report.evaluation is None
                or (
                    report.evaluation.decision != "FAIL"
                    and not (report.fraud_evaluation and report.fraud_evaluation.status == "FAIL")
                )
                or report.simulated_outcomes
                or (
                    self.config.tenant_id == "verification"
                    and not self.policy.allow_verification_jobs
                )
            ):
                continue
            live = await asyncio.to_thread(self.guard, report)
            positives = sum(row["actual"] for row in live)
            if (
                len(live) < self.policy.min_verified_labels
                or min(positives, len(live) - positives) < self.policy.min_class_samples
            ):
                continue
            production_fit, live_guard = partition_verified_rows(live)
            guard_positives = sum(row["actual"] for row in live_guard)
            if (
                len(live_guard) < self.policy.min_guard_labels
                or min(guard_positives, len(live_guard) - guard_positives)
                < self.policy.min_guard_class_samples
            ):
                continue
            scheduled = await asyncio.to_thread(
                self.store.schedule, version, report.report_id, self.policy.model_dump(mode="json")
            )
            if scheduled:
                await self.train(scheduled, live_guard, production_fit)

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.cycle()
                self.error = None
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                self.error = (
                    "Lifecycle data/configuration unavailable; serving monitoring continues"
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self.policy.interval_seconds)
