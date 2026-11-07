"""Independent background jobs: slow model/LLM work never blocks live monitoring."""

import asyncio
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from agent6_mlops_lifecycle.assurance.advisor import PROMPT_VERSION, explain
from agent6_mlops_lifecycle.assurance.benchmark import run_benchmarks
from agent6_mlops_lifecycle.assurance.interpretation import eligible_checks
from agent6_mlops_lifecycle.assurance.models import BenchmarkReport
from agent6_mlops_lifecycle.assurance.store import AssuranceStore, encode
from agent6_mlops_lifecycle.evaluation.llm_judge import LLMJudgeSettings
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.state import MonitorState


class AssuranceService:
    def __init__(self, settings: MonitoringSettings, monitor: MonitorState) -> None:
        self.settings, self.monitor = settings, monitor
        self.enabled = settings.benchmark_registry is not None or settings.advisor_enabled
        self.store = (
            AssuranceStore(settings.benchmark_database, settings.scope()) if self.enabled else None
        )
        self.error: str | None = None
        self._advisor_lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        try:
            return self._snapshot()
        except (ValueError, sqlite3.Error, OSError):
            return {
                "benchmarks": [],
                "advisor": {"status": "ERROR"},
                "error": "Assurance evidence is unavailable or failed integrity validation",
            }

    def _snapshot(self) -> dict[str, Any]:
        if self.store is None:
            return {"benchmarks": [], "advisor": {"status": "DISABLED"}, "error": self.error}
        benchmarks = []
        for job in self.store.jobs("benchmark"):
            result = dict(job)
            reference = job.get("report_ref")
            if reference:
                result["report"] = BenchmarkReport.model_validate(
                    self.store.get("benchmark", reference)
                ).model_dump(mode="json")
            benchmarks.append(result)
        advisors = self.store.jobs("advisor")
        if not self.settings.advisor_enabled:
            advisor = {"status": "DISABLED"}
        elif not LLMJudgeSettings().groq_api_key.get_secret_value():
            advisor = {
                "status": "NOT_CONFIGURED",
                "detail": "Add GROQ_API_KEY to Agent 6 .env; deterministic checks remain active",
            }
        elif advisors:
            advisor = dict(advisors[0])
            if advisor.get("report_ref"):
                advisor["report"] = self.store.get("advisor", advisor["report_ref"])
        else:
            advisor = {"status": "WAITING_FOR_EVIDENCE"}
        return {
            "benchmarks": benchmarks,
            "advisor": advisor,
            "error": self.error,
            "benchmark_interval_seconds": self.settings.benchmark_interval_seconds,
        }

    def context(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        snapshot = self.snapshot()
        included_versions: set[str] = set()
        for job in snapshot["benchmarks"][:8]:
            if job.get("model_version") in included_versions:
                continue
            included_versions.add(job.get("model_version", ""))
            if report := job.get("report"):
                evidence["sha256:" + job["report_ref"]] = {
                    "kind": "LABELED_BENCHMARK",
                    "artifact_name": report["artifact_name"],
                    "model_version": report["model_version"],
                    "assessed_at": report["assessed_at"],
                    "training_independence": report["training_independence"],
                    "production_correctness": "UNKNOWN",
                    "label_source": report["label_source"],
                    "label_counts": report["label_counts"],
                    "groups": {
                        name: {
                            "sample_count": value["sample_count"],
                            "decision": value["decision"],
                            "metrics": value["metrics"],
                            "gate_results": value["gate_results"],
                        }
                        for name, value in report["evaluations"].items()
                    },
                    "limitations": report["limitations"],
                }
        heartbeat = self.monitor.status()
        if heartbeat and heartbeat.services:
            probes = {
                "kind": "CURRENT_SERVICE_HEALTH",
                "services": {name: probe.state for name, probe in heartbeat.services.items()},
                "limitation": "An UP endpoint is not evidence that predictions are correct.",
            }
            reference = (
                self.store.put("service-checks", probes)
                if self.store
                else sha256(encode(probes)).hexdigest()
            )
            evidence["sha256:" + reference] = probes
        for version in heartbeat.active_versions if heartbeat else ():
            report = self.monitor.latest(version)
            if report:
                evaluation = report.evaluation
                evidence["sha256:" + report.report_id] = {
                    "kind": "LIVE_MONITOR",
                    "evidence_origin": "SCRIPTED_VERIFICATION_FIXTURES"
                    if self.settings.tenant_id == "verification"
                    else "LIVE_SERVING",
                    "model_version": version,
                    "assessed_at": report.assessed_at.isoformat(),
                    "verified_outcomes": report.system_outcomes + report.human_outcomes,
                    "simulated_outcomes": report.simulated_outcomes,
                    "quality_decision": evaluation.decision if evaluation else "UNKNOWN",
                    "metrics": evaluation.metrics if evaluation else {},
                    "gate_results": [
                        gate.model_dump(mode="json") for gate in evaluation.gate_results
                    ]
                    if evaluation
                    else [],
                    "policy_is_calibrated": report.policy_is_calibrated,
                    "drift": report.drift.model_dump(mode="json"),
                    "drift_interpretation": "UNKNOWN"
                    if report.drift.status in ("NO_BASELINE", "INSUFFICIENT_EVIDENCE")
                    else report.drift.status,
                    "feature_drift": report.feature_drift.model_dump(mode="json")
                    if report.feature_drift
                    else None,
                    "operational_health": report.operational_health.model_dump(mode="json")
                    if report.operational_health
                    else None,
                }
        return evidence

    async def advise(self, *, force: bool = False) -> None:
        # Background and presentation requests share one lock and the same evidence cache.
        async with self._advisor_lock:
            await self._advise(force=force)

    async def _advise(self, *, force: bool) -> None:
        if (
            not self.settings.advisor_enabled
            or self.store is None
            or not LLMJudgeSettings().groq_api_key.get_secret_value()
        ):
            return
        evidence = await asyncio.to_thread(self.context)
        if not evidence or not eligible_checks(evidence):
            return
        key = sha256(
            encode(
                {
                    "evidence": evidence,
                    "prompt_version": PROMPT_VERSION,
                    "model": self.settings.advisor_model,
                }
            )
        ).hexdigest()
        existing = await asyncio.to_thread(self.store.job, key)
        if existing and existing["status"] == "COMPLETED":
            return
        now = datetime.now(UTC)
        # Rate limit across evidence changes and process restarts, not just per hash.
        previous = await asyncio.to_thread(self.store.jobs, "advisor")
        if (
            not force
            and previous
            and previous[0].get("prompt_version") == PROMPT_VERSION
            and (now - datetime.fromisoformat(previous[0]["updated_at"])).total_seconds()
            < self.settings.advisor_interval_seconds
        ):
            return
        context_ref = await asyncio.to_thread(self.store.put, "advisor-context", evidence)
        checkpoint: dict[str, Any] = {
            "context_ref": context_ref,
            "status": "RUNNING",
            "provider": "groq",
            "model": self.settings.advisor_model,
            "prompt_version": PROMPT_VERSION,
            "evidence_ids": list(evidence),
            "advisory_only": True,
            "live_correctness": "UNKNOWN_UNLESS_SUPPORTED_BY_VERIFIED_OUTCOMES",
            "training_independence": "UNVERIFIED",
        }
        await asyncio.to_thread(self.store.save_job, key, "advisor", now, checkpoint)
        try:
            result = await explain(self.settings, evidence)
            reference = await asyncio.to_thread(
                self.store.put,
                "advisor",
                {
                    **checkpoint,
                    "status": "COMPLETED",
                    "explanation": result.model_dump(mode="json"),
                },
            )
            checkpoint.update(status="COMPLETED", report_ref=reference)
        except (RuntimeError, ValueError, OSError):
            checkpoint.update(
                status="ERROR",
                error="LLM explanation unavailable; deterministic checks remain authoritative",
            )
        await asyncio.to_thread(self.store.save_job, key, "advisor", datetime.now(UTC), checkpoint)

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                if self.store is not None:
                    await run_benchmarks(self.settings, self.store)
                    await self.advise()
                self.error = None
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                self.error = (
                    "Assurance configuration or evidence is unavailable; live monitoring continues"
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=5)
