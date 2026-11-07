"""Latency, throughput and reliability benchmark of the closed loop (v5 section 18).

    python -m aiops_platform.experiments.benchmark                     # 10 incidents, no faults
    python -m aiops_platform.experiments.benchmark -n 30 --duplicates 0.3 --context-failures 0.15
    python -m aiops_platform.experiments.benchmark --json report.json

Each run opens N concurrent incidents (N services each running a leaking
release), lets the platform diagnose and plan, approves every request as the
SRE (instantly, so human time is excluded), and waits for verified recovery.

Fault injection (`--duplicates`, `--context-failures`) exercises the
reliability controls: every published record is redelivered with probability
p (Kafka at-least-once), and every agent write to the Context Service fails
with probability q (a dependency outage the consumers must retry). The run
then checks the safety properties: exactly one infrastructure change per
incident, no lost workflow, valid audit chains.

What the numbers mean: everything runs in one process on the simulated
cluster, with Agent 1's verification windows collapsed to zero, so latencies
measure the platform and agents' own processing (decisions, Context writes,
signing, Agent 4's coordinator), not observation windows or network hops.
Use the distributed stack (`compose.platform.yaml` + Grafana) for those.
"""

import argparse
import asyncio
import json
import random
import statistics
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from aiops_platform.bus import InMemoryBus, Record, TransientError
from aiops_platform.context.client import AgentContext
from aiops_platform.context.service import Principal
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.hitl.service import Operator
from aiops_platform.observability.metrics import PlatformMetrics
from aiops_platform.runtime import Platform
from aiops_platform.sim.cluster import Release, SimulatedCluster, digest_image

SRE = Operator(subject="bench@sre", roles=frozenset({"sre"}))
ORCH = Principal.orchestrator()


class ChaosBus(InMemoryBus):
    """In-memory bus that redelivers records like an at-least-once broker."""

    def __init__(self, duplicate_rate: float, rng: random.Random) -> None:
        super().__init__()
        self._rate = duplicate_rate
        self._rng = rng
        self.injected_duplicates = 0

    async def publish_record(self, record: Record) -> None:
        await super().publish_record(record)
        if not record.topic.endswith(".dlq") and self._rng.random() < self._rate:
            self.injected_duplicates += 1
            await super().publish_record(record)


class FlakyAgentContext(AgentContext):
    """An agent's Context client during an intermittent Context Service outage."""

    failures = 0

    def __init__(self, service, agent_id, *, rate: float, rng: random.Random) -> None:
        super().__init__(service, agent_id)
        self._rate = rate
        self._rng = rng

    def _maybe_fail(self) -> None:
        if self._rng.random() < self._rate:
            FlakyAgentContext.failures += 1
            raise TransientError("CONTEXT_UNAVAILABLE (injected)")

    async def append_agent_result(self, result, result_type):
        self._maybe_fail()
        return await super().append_agent_result(result, result_type)

    async def append_evidence(self, **kwargs):
        self._maybe_fail()
        return await super().append_evidence(**kwargs)


class RecordingMetrics(PlatformMetrics):
    """PlatformMetrics that also keeps raw samples, for exact percentiles."""

    def __init__(self) -> None:
        super().__init__()
        self.task_samples: dict[str, list[float]] = defaultdict(list)

    def task_done(self, agent: str, objective: str, status: str, seconds: float) -> None:
        super().task_done(agent, objective, status, seconds)
        self.task_samples[f"{agent}:{objective}"].append(seconds)


def fleet(n: int) -> tuple[SimulatedCluster, list[ServiceIdentity]]:
    cluster = SimulatedCluster()
    services = []
    for i in range(1, n + 1):
        name = f"svc-{i:02d}"
        repo = f"registry.local/{name}"
        cluster.add_service(
            name,
            "prod",
            [
                Release("v1", digest_image(repo, "v1")),
                Release("v2", digest_image(repo, "v2"), frozenset({"memory_leak"})),
            ],
            current="v1",
        )
        services.append(ServiceIdentity(service_id=name, namespace="prod", version="v2"))
    for s in services:
        cluster.release_version(s.service_id, "prod", "v2")
    cluster.advance(9)
    return cluster, services


def summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None, "mean": None}
    ordered = sorted(values)

    def pct(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))], 4)

    return {
        "n": len(values),
        "p50": pct(0.5),
        "p95": pct(0.95),
        "max": round(ordered[-1], 4),
        "mean": round(statistics.mean(values), 4),
    }


def counter_total(counter) -> float:
    return sum(
        sample.value for metric in counter.collect() for sample in metric.samples if sample.name.endswith("_total")
    )


async def run(n: int, duplicate_rate: float, context_failure_rate: float, seed: int, workdir: Path) -> dict[str, Any]:
    rng = random.Random(seed)
    cluster, services = fleet(n)
    metrics = RecordingMetrics()
    bus = ChaosBus(duplicate_rate, rng)
    FlakyAgentContext.failures = 0

    def agent_context(service, agent_id):
        return FlakyAgentContext(service, agent_id, rate=context_failure_rate, rng=rng)

    platform = await Platform.in_memory(
        workdir,
        cluster,
        watched=services,
        metrics=metrics,
        bus=bus,
        agent_context=agent_context if context_failure_rate > 0 else None,
    )
    started = time.perf_counter()
    try:
        incidents = await platform.watcher.tick()
        await platform.bus.drain(timeout=600)
        decided_at = time.perf_counter()
        approvals = await platform.pending_approvals()
        approved_at: dict[str, float] = {}
        for approval in approvals:
            approved_at[approval["incident_id"]] = time.perf_counter()
            await platform.orchestrator.resolve_approval(
                approval["approval_id"], decision="APPROVE", operator=SRE, comment="benchmark"
            )
        await platform.bus.drain(timeout=600)
        # Anything still waiting lost a message: let the timeout path settle it.
        platform.orchestrator.task_timeout_seconds = 0
        await platform.orchestrator.sweep()
        await platform.bus.drain(timeout=600)
        finished = time.perf_counter()

        rows, stages = [], defaultdict(int)
        for incident in incidents:
            ctx = await platform.context.get_incident_context(ORCH, incident)
            trail = await platform.context.audit_trail(ORCH, incident)
            status = await platform.orchestrator.status(incident)
            stages[status.get("stage", "?")] += 1

            def first(action, stage=None, trail=trail):
                for r in trail:
                    if r["action"] == action and (
                        stage is None
                        or r["detail_json"].get("stage") == stage
                        or r["detail_json"].get("status") == stage
                    ):
                        return r["created_at"]
                return None

            opened = ctx.opened_at
            decision = first("DECISION_RECORDED", "POLICY")
            waiting = first("APPROVAL_WAITING_FOR_HUMAN")
            approved = first("APPROVAL_APPROVED")
            resolved = first("INCIDENT_UPDATED", "RESOLVED")
            rows.append(
                {
                    "incident_id": incident,
                    "stage": status.get("stage"),
                    "root_cause": (status.get("consensus") or {}).get("primary_root_cause"),
                    "detect_to_decision_s": (decision - opened).total_seconds() if decision else None,
                    "decision_to_approval_request_s": (waiting - decision).total_seconds()
                    if waiting and decision
                    else None,
                    "approval_to_recovered_s": (resolved - approved).total_seconds() if resolved and approved else None,
                    "audit_chain_valid": await platform.context.verify_audit_chain(ORCH, incident),
                    "mutations": sum(1 for m in cluster.mutations if m["service"] == ctx.service.service_id),
                }
            )
        resolved_rows = [r for r in rows if r["stage"] == "RESOLVED"]
        diagnosed = [r for r in rows if r["root_cause"]]
        published = len(bus.published)
        dead = sum(1 for r in bus.published if r.topic.endswith(".dlq"))
        return {
            "config": {
                "incidents": n,
                "duplicate_rate": duplicate_rate,
                "context_failure_rate": context_failure_rate,
                "seed": seed,
            },
            "latency_seconds": {
                "detect_to_decision": summary(
                    [r["detect_to_decision_s"] for r in rows if r["detect_to_decision_s"] is not None]
                ),
                "approval_to_verified_recovery": summary(
                    [r["approval_to_recovered_s"] for r in resolved_rows if r["approval_to_recovered_s"] is not None]
                ),
                "agent_tasks": {k: summary(v) for k, v in sorted(metrics.task_samples.items())},
            },
            "throughput": {
                "wall_seconds": round(finished - started, 3),
                "diagnosis_phase_seconds": round(decided_at - started, 3),
                "incidents_resolved_per_minute": round(len(resolved_rows) / ((finished - started) / 60), 2)
                if resolved_rows
                else 0.0,
            },
            "reliability": {
                "resolved": len(resolved_rows),
                "by_stage": dict(stages),
                "root_cause_accuracy": round(
                    sum(r["root_cause"] == "MEMORY_LEAK_POST_DEPLOY" for r in diagnosed) / len(diagnosed), 3
                )
                if diagnosed
                else None,
                "exactly_one_change_per_resolved_incident": all(r["mutations"] == 1 for r in resolved_rows),
                "no_change_without_resolution": all(r["mutations"] <= 1 for r in rows),
                "audit_chains_valid": all(r["audit_chain_valid"] for r in rows),
                "published_records": published,
                "injected_duplicates": bus.injected_duplicates,
                "duplicates_ignored": counter_total(metrics.duplicates),
                "injected_context_failures": FlakyAgentContext.failures,
                "retries": counter_total(metrics.retries),
                "dead_letters": dead,
                "message_reliability": round(1 - dead / published, 5) if published else None,
            },
            "per_incident": rows,
        }
    finally:
        await platform.close()


def print_report(report: dict[str, Any]) -> None:
    c, lat, thr, rel = report["config"], report["latency_seconds"], report["throughput"], report["reliability"]
    print(
        f"\n== {c['incidents']} incidents | duplicates {c['duplicate_rate']:.0%} | Context failures {c['context_failure_rate']:.0%} =="
    )
    print("Latency (seconds)            n     p50      p95      max")
    for name in ("detect_to_decision", "approval_to_verified_recovery"):
        s = lat[name]
        print(f"  {name:<26} {s['n']:>3} {s['p50'] or 0:>8.3f} {s['p95'] or 0:>8.3f} {s['max'] or 0:>8.3f}")
    print("  agent tasks:")
    for name, s in lat["agent_tasks"].items():
        print(f"    {name:<46} {s['n']:>3} {s['p50']:>8.3f} {s['p95']:>8.3f} {s['max']:>8.3f}")
    print(f"Throughput: {thr['incidents_resolved_per_minute']} incidents/min (wall {thr['wall_seconds']} s)")
    print(
        f"Reliability: {rel['resolved']}/{c['incidents']} resolved {rel['by_stage']}, RCA accuracy {rel['root_cause_accuracy']}, "
        f"one change per incident={rel['exactly_one_change_per_resolved_incident']}, audit chains valid={rel['audit_chains_valid']}"
    )
    print(
        f"  messages {rel['published_records']}, injected duplicates {rel['injected_duplicates']} -> ignored {rel['duplicates_ignored']:.0f}, "
        f"injected Context failures {rel['injected_context_failures']} -> retries {rel['retries']:.0f}, dead letters {rel['dead_letters']}, "
        f"message reliability {rel['message_reliability']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--incidents", type=int, default=10)
    parser.add_argument("--duplicates", type=float, default=0.0, help="redelivery probability per record")
    parser.add_argument(
        "--context-failures", type=float, default=0.0, help="failure probability per agent Context write"
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args(argv)
    import logging
    import os

    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    logging.basicConfig(level=logging.CRITICAL)
    # Agent 4's SQLite journal stays open until exit; Windows cannot delete it earlier.
    with tempfile.TemporaryDirectory(prefix="aiops-bench-", ignore_cleanup_errors=True) as workdir:
        report = asyncio.run(run(args.incidents, args.duplicates, args.context_failures, args.seed, Path(workdir)))
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"full report: {args.json}")
    rel = report["reliability"]
    safe = (
        rel["no_change_without_resolution"]
        and rel["exactly_one_change_per_resolved_incident"]
        and rel["audit_chains_valid"]
    )
    return 0 if safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
