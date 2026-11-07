"""In-process composition root: the whole platform on one event loop.

Used by the end-to-end tests and the offline demo (`python -m aiops_platform.demo`).
Every component is the production code path; only the transports are
in-memory (bus, SQLite) and the infrastructure is the simulated cluster.
`compose.platform.yaml` runs the same components as separate services on
Redpanda/PostgreSQL.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from aiops_platform.bus import InMemoryBus
from aiops_platform.context.client import AgentContext, OrchestratorContext
from aiops_platform.context.objects import LocalObjectStore
from aiops_platform.context.service import ContextService, Principal
from aiops_platform.context.store import ContextStore
from aiops_platform.contracts.enums import AgentId, Environment
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.hitl.keys import SigningKey, registry_for
from aiops_platform.hitl.service import HitlService
from aiops_platform.orchestrator.service import HitlClient, OrchestratorService
from aiops_platform.sim.cluster import SimInspector, SimMutationAdapter, SimulatedCluster
from aiops_platform.workers.agent1 import Agent1VerdictService, MonitoringWorker, SimWatcher, build_sim_monitoring
from aiops_platform.workers.agent2 import DiagnosisWorker, sim_toolbox
from aiops_platform.workers.agent3 import SecurityQualityWorker
from aiops_platform.workers.agent4 import DeploymentWorker, PlanStore
from aiops_platform.workers.agent4_executor import Agent1VerdictProvider, Agent4Executor, ClusterTargetResolver
from aiops_platform.workers.agent5 import ResourceOptimizationWorker, cluster_metrics
from aiops_platform.workers.agent6 import MLOpsWorker


@dataclass
class Platform:
    bus: InMemoryBus
    store: ContextStore
    context: ContextService
    hitl: HitlService
    orchestrator: OrchestratorService
    cluster: SimulatedCluster
    watcher: SimWatcher
    agent4: Agent4Executor

    @classmethod
    async def in_memory(
        cls,
        workdir: Path,
        cluster: SimulatedCluster,
        *,
        watched: list[ServiceIdentity],
        environment: Environment = Environment.PRODUCTION,
    ) -> "Platform":
        workdir.mkdir(parents=True, exist_ok=True)
        bus = InMemoryBus()
        store = await ContextStore.connect(
            f"sqlite+aiosqlite:///{workdir / 'context.db'}", LocalObjectStore(workdir / "evidence")
        )
        context = ContextService(store)
        hitl_key = SigningKey.load_or_create(workdir / "keys/hitl.pem", "hitl-key-1", "aiops-hitl-service")
        agent1_key = SigningKey.load_or_create(workdir / "keys/agent1.pem", "agent1-key-1", "agent1-monitoring")
        hitl = HitlService(context=context, signer=hitl_key)
        orchestrator = OrchestratorService(bus=bus, context=OrchestratorContext(context), hitl=HitlClient(hitl))

        agent, publisher, detector, verifier = build_sim_monitoring(cluster)
        loop = asyncio.get_running_loop()

        def approval_lookup(approval_id: str):
            # Agent 4's coordinator runs in a worker thread.
            return asyncio.run_coroutine_threadsafe(hitl.provenance_record(approval_id), loop).result(30)

        executor = Agent4Executor(
            journal_path=workdir / "agent4-journal.db",
            inspector=SimInspector(cluster),
            mutation_adapter=SimMutationAdapter(cluster),
            resolver=ClusterTargetResolver(cluster),
            preflight_policy=cluster.preflight_policy,
            hitl_registry=registry_for(hitl_key),
            agent1_registry=registry_for(agent1_key),
            approval_lookup=approval_lookup,
            verdicts=Agent1VerdictProvider(Agent1VerdictService(verifier, agent1_key).sync_provider()),
        )
        workers = [
            MonitoringWorker(bus, AgentContext(context, AgentId.MONITORING), agent=agent, publisher=publisher),
            DiagnosisWorker(bus, AgentContext(context, AgentId.DIAGNOSIS), toolbox=sim_toolbox(cluster)),
            SecurityQualityWorker(bus, AgentContext(context, AgentId.SECURITY_QUALITY)),
            DeploymentWorker(
                bus,
                AgentContext(context, AgentId.DEPLOYMENT_RECOVERY),
                executor=executor,
                plans=PlanStore(workdir / "plans"),
            ),
            ResourceOptimizationWorker(
                bus, AgentContext(context, AgentId.RESOURCE_OPTIMIZATION), metrics=cluster_metrics(cluster)
            ),
            MLOpsWorker(bus, AgentContext(context, AgentId.MLOPS_LIFECYCLE), output_dir=workdir / "agent6"),
        ]
        orchestrator.subscribe()
        for worker in workers:
            worker.subscribe()
        await bus.start()
        watcher = SimWatcher(bus, cluster, detector, watched, environment)
        return cls(bus, store, context, hitl, orchestrator, cluster, watcher, executor)

    async def pending_approvals(self) -> list[dict]:
        return await self.context.list_approvals(Principal.dashboard(), state="WAITING_FOR_HUMAN")

    async def close(self) -> None:
        await self.bus.stop()
        await self.store.close()
