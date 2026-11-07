"""Process entry points for the distributed deployment (`compose.platform.yaml`).

    python -m aiops_platform.services context        Context Service (gRPC, PostgreSQL)
    python -m aiops_platform.services hitl           HITL Service (gRPC)
    python -m aiops_platform.services orchestrator   Orchestrator (Kafka + LangGraph/Postgres + HTTP API)
    python -m aiops_platform.services worker <agent> one agent worker (Kafka + Context gRPC)
    python -m aiops_platform.services dashboard      Operator Dashboard (HTTP)
    python -m aiops_platform.services sim-cluster    simulated cluster + telemetry (HTTP)

Configuration is environment-only (see `compose.platform.yaml`). Each process
holds only its own service credential and, where it signs, its own private key;
public keys are shared through a read-only trust directory.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import grpc

from aiops_platform.context.client import AgentContext, OrchestratorContext
from aiops_platform.context.objects import LocalObjectStore
from aiops_platform.context.service import ContextService, Role
from aiops_platform.context.store import ContextStore
from aiops_platform.contracts.enums import AgentId, Environment
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.observability.metrics import PlatformMetrics, serve_metrics
from aiops_platform.rpc.identity import Credential, Verifier

log = logging.getLogger("aiops.services")

# One registry per process; scraped by Prometheus (see docker/prometheus.yml).
METRICS = PlatformMetrics()


def expose_metrics() -> None:
    serve_metrics(METRICS, int(env("METRICS_PORT", "9100")))


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing environment variable {name}")
    return value


def verifier() -> Verifier:
    return Verifier(env("AIOPS_SERVICE_SECRET"))


def channel(target_var: str) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(env(target_var))


async def wait_for_file(path: Path, timeout: float = 120) -> Path:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() > deadline:
            raise SystemExit(f"timed out waiting for {path}")
        await asyncio.sleep(1)
    return path


def make_bus(client_id: str):
    from aiops_platform.bus.kafka import KafkaBus

    return KafkaBus(
        env("KAFKA_BOOTSTRAP"), client_id=client_id, partitions=int(env("KAFKA_PARTITIONS", "3")), metrics=METRICS
    )


def make_idempotency():
    url = os.environ.get("REDIS_URL")
    if not url:
        from aiops_platform.bus.idempotency import InMemoryIdempotencyStore

        return InMemoryIdempotencyStore()
    import redis.asyncio as redis

    from aiops_platform.bus.idempotency import RedisIdempotencyStore

    return RedisIdempotencyStore(redis.from_url(url))


async def serve_grpc(server: grpc.aio.Server, address: str) -> None:
    server.add_insecure_port(address)
    await server.start()
    log.info("gRPC listening on %s", address)
    await server.wait_for_termination()


# ---- roles -------------------------------------------------------------------


async def run_context() -> None:
    from aiops_platform.rpc.context_rpc import add_context_service

    store = await ContextStore.connect(
        env("CONTEXT_DB_URL"), LocalObjectStore(env("CONTEXT_OBJECTS_DIR", "/data/evidence"))
    )
    expose_metrics()
    server = grpc.aio.server()
    add_context_service(server, ContextService(store), verifier(), METRICS)
    await serve_grpc(server, env("GRPC_LISTEN", "0.0.0.0:50051"))


async def run_hitl() -> None:
    from aiops_platform.hitl.keys import SigningKey
    from aiops_platform.hitl.service import HitlService
    from aiops_platform.rpc.context_rpc import GrpcContextService
    from aiops_platform.rpc.hitl_rpc import add_hitl_service

    key = SigningKey.load_or_create(env("HITL_KEY_PATH", "/secrets/hitl.pem"), "hitl-key-1", "aiops-hitl-service")
    trust = Path(env("TRUST_DIR", "/trust"))
    trust.mkdir(parents=True, exist_ok=True)
    (trust / "hitl.pub.pem").write_text(key.public_pem())
    context = GrpcContextService(channel("CONTEXT_TARGET"), Credential.from_env(Role.HITL, "hitl-service"))
    expose_metrics()
    server = grpc.aio.server()
    add_hitl_service(server, HitlService(context=context, signer=key), verifier())
    await serve_grpc(server, env("GRPC_LISTEN", "0.0.0.0:50052"))


async def run_orchestrator() -> None:
    import uvicorn

    from aiops_platform.orchestrator.api import create_orchestrator_api
    from aiops_platform.orchestrator.service import OrchestratorService
    from aiops_platform.rpc.context_rpc import GrpcContextService
    from aiops_platform.rpc.hitl_rpc import GrpcHitlClient

    credential = Credential.from_env(Role.ORCHESTRATOR, "orchestrator")
    context = OrchestratorContext(GrpcContextService(channel("CONTEXT_TARGET"), credential))
    hitl = GrpcHitlClient(channel("HITL_TARGET"), credential)
    bus = make_bus("orchestrator")
    checkpoint_url = os.environ.get("CHECKPOINT_DB_URL")
    if checkpoint_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver_cm = AsyncPostgresSaver.from_conn_string(checkpoint_url)
        saver = await saver_cm.__aenter__()
        await saver.setup()
    else:
        from langgraph.checkpoint.memory import InMemorySaver

        saver = InMemorySaver()
    orchestrator = OrchestratorService(
        bus=bus, context=context, hitl=hitl, checkpointer=saver, idempotency=make_idempotency(), metrics=METRICS
    )
    orchestrator.subscribe()
    await bus.start()

    async def sweep_forever():
        while True:
            await asyncio.sleep(15)
            try:
                await orchestrator.sweep()
            except Exception:  # noqa: BLE001 - keep sweeping
                log.exception("sweep failed")

    sweeper = asyncio.create_task(sweep_forever())
    app = create_orchestrator_api(orchestrator, verifier())
    config = uvicorn.Config(app, host="0.0.0.0", port=int(env("HTTP_PORT", "8080")), log_level="info")
    try:
        await uvicorn.Server(config).serve()
    finally:
        sweeper.cancel()
        await bus.stop()


async def run_worker(agent: str) -> None:
    agent_id = AgentId(agent if agent.endswith("-agent") else f"{agent}-agent")
    credential = Credential.from_env(Role.AGENT, agent_id.value)
    from aiops_platform.rpc.context_rpc import GrpcContextService

    context = AgentContext(GrpcContextService(channel("CONTEXT_TARGET"), credential), agent_id)
    bus = make_bus(agent_id.value)
    kwargs = {"idempotency": make_idempotency(), "metrics": METRICS}
    expose_metrics()
    cluster = None
    if os.environ.get("SIM_CLUSTER_URL"):
        from aiops_platform.sim.remote import RemoteCluster

        cluster = RemoteCluster(env("SIM_CLUSTER_URL"))
    extra_tasks = []

    if agent_id is AgentId.MONITORING:
        from aiops_platform.hitl.keys import SigningKey
        from aiops_platform.rpc.hitl_rpc import add_monitoring_service
        from aiops_platform.workers.agent1 import (
            Agent1VerdictService,
            MonitoringWorker,
            SimWatcher,
            build_sim_monitoring,
        )

        agent, publisher, detector, verifier_ = build_sim_monitoring(
            cluster,
            window_seconds=float(env("VERIFY_WINDOW_SECONDS", "60")),
            sample_seconds=float(env("VERIFY_SAMPLE_SECONDS", "15")),
            sleep=asyncio.sleep,
        )
        worker = MonitoringWorker(bus, context, agent=agent, publisher=publisher, **kwargs)
        key = SigningKey.load_or_create(
            env("AGENT1_KEY_PATH", "/secrets/agent1.pem"), "agent1-key-1", "agent1-monitoring"
        )
        trust = Path(env("TRUST_DIR", "/trust"))
        trust.mkdir(parents=True, exist_ok=True)
        (trust / "agent1.pub.pem").write_text(key.public_pem())
        server = grpc.aio.server()
        add_monitoring_service(server, Agent1VerdictService(verifier_, key), verifier(), health=cluster.signals)
        extra_tasks.append(serve_grpc(server, env("GRPC_LISTEN", "0.0.0.0:50053")))
        watched = [
            ServiceIdentity(service_id=s.split("/")[1], namespace=s.split("/")[0], version="unknown")
            for s in env("WATCH_SERVICES", "prod/payment-service,prod/checkout-api").split(",")
        ]
        watcher = SimWatcher(bus, cluster, detector, watched, Environment(env("WATCH_ENVIRONMENT", "production")))

        async def watch_forever():
            while True:
                await asyncio.sleep(float(env("WATCH_INTERVAL_SECONDS", "10")))
                try:
                    await watcher.tick()
                except Exception:  # noqa: BLE001 - one failed cycle must not stop monitoring
                    log.exception("watch cycle failed")

        extra_tasks.append(watch_forever())
    elif agent_id is AgentId.DIAGNOSIS:
        from aiops_platform.workers.agent2 import DiagnosisWorker, live_toolbox, sim_toolbox

        worker = DiagnosisWorker(bus, context, toolbox=sim_toolbox(cluster) if cluster else live_toolbox(), **kwargs)
    elif agent_id is AgentId.SECURITY_QUALITY:
        from aiops_platform.workers.agent3 import SecurityQualityWorker

        worker = SecurityQualityWorker(
            bus, context, allow_local_scan=env("AGENT3_LOCAL_SCAN", "false") == "true", **kwargs
        )
    elif agent_id is AgentId.DEPLOYMENT_RECOVERY:
        from agent4_deployment_recovery.adapters.signed_contracts import TrustedIssuerKey, TrustedIssuerRegistry
        from aiops_platform.hitl.keys import AGENT4_AUDIENCE
        from aiops_platform.rpc.hitl_rpc import SyncHitlProvenance, SyncVerdictClient
        from aiops_platform.sim.cluster import SimInspector, SimMutationAdapter
        from aiops_platform.workers.agent4 import DeploymentWorker, PlanStore
        from aiops_platform.workers.agent4_executor import Agent1VerdictProvider, Agent4Executor, ClusterTargetResolver

        trust = Path(env("TRUST_DIR", "/trust"))

        def registry(file: str, key_id: str, issuer: str) -> TrustedIssuerRegistry:
            return TrustedIssuerRegistry(
                keys=(
                    TrustedIssuerKey(
                        key_id=key_id,
                        issuer=issuer,
                        audience=AGENT4_AUDIENCE,
                        public_key_pem=(trust / file).read_text(),
                    ),
                )
            )

        await wait_for_file(trust / "hitl.pub.pem")
        await wait_for_file(trust / "agent1.pub.pem")
        executor = Agent4Executor(
            journal_path=Path(env("AGENT4_JOURNAL", "/data/agent4-journal.db")),
            inspector=SimInspector(cluster),
            mutation_adapter=SimMutationAdapter(cluster),
            resolver=ClusterTargetResolver(cluster),
            preflight_policy=cluster.preflight_policy,
            hitl_registry=registry("hitl.pub.pem", "hitl-key-1", "aiops-hitl-service"),
            agent1_registry=registry("agent1.pub.pem", "agent1-key-1", "agent1-monitoring"),
            approval_lookup=SyncHitlProvenance(env("HITL_TARGET"), credential),
            verdicts=Agent1VerdictProvider(SyncVerdictClient(env("MONITORING_TARGET"), credential)),
        )
        worker = DeploymentWorker(
            bus, context, executor=executor, plans=PlanStore(Path(env("AGENT4_PLANS", "/data/plans"))), **kwargs
        )
    elif agent_id is AgentId.RESOURCE_OPTIMIZATION:
        from aiops_platform.workers.agent5 import ResourceOptimizationWorker, cluster_metrics

        worker = ResourceOptimizationWorker(
            bus, context, metrics_source=cluster_metrics(cluster) if cluster else None, **kwargs
        )
    else:
        from aiops_platform.workers.agent6 import MLOpsWorker

        worker = MLOpsWorker(bus, context, output_dir=Path(env("AGENT6_OUTPUT", "/data/agent6")), **kwargs)

    worker.subscribe()
    await bus.start()
    log.info("%s worker started", agent_id.value)
    try:
        await asyncio.gather(*extra_tasks) if extra_tasks else await asyncio.Event().wait()
    finally:
        await bus.stop()


async def run_dashboard() -> None:
    import uvicorn

    from aiops_platform.dashboard.app import DEFAULT_OPERATORS, create_app, load_operators
    from aiops_platform.orchestrator.api import HttpOrchestrator
    from aiops_platform.rpc.context_rpc import GrpcContextService

    credential = Credential.from_env(Role.DASHBOARD, "dashboard")
    orchestrator = HttpOrchestrator(env("ORCHESTRATOR_URL"), credential)
    scenarios = None
    if os.environ.get("SIM_CLUSTER_URL"):
        from aiops_platform.sim.remote import RemoteCluster
        from aiops_platform.sim.scenarios import model_check_request, release_v28_request

        cluster = RemoteCluster(env("SIM_CLUSTER_URL"))

        def inject(name):
            async def run():
                return [str(cluster.scenario(name))]

            return run

        # The same scenarios as `demo serve`: faults go to the simulated cluster (Agent 1
        # detects them); releases and model checks start a workflow at the Orchestrator.
        def release(fixed: bool):
            async def run():
                request = release_v28_request(cluster, fixed)
                await orchestrator.start_release(**request)
                return [request["release_id"]]

            return run

        async def model_check():
            request = model_check_request()
            await orchestrator.start_model_check(**request)
            return [request["check_id"]]

        scenarios = {name: inject(name) for name in ("memory_leak_after_deploy", "cpu_load_spike", "reset")}
        scenarios |= {
            "model_drift_check": model_check,
            "release_v28_failing_test": release(False),
            "release_v28_fixed": release(True),
        }
    app = create_app(
        context=GrpcContextService(channel("CONTEXT_TARGET"), credential),
        orchestrator=orchestrator,
        operators=load_operators(Path(env("OPERATORS_FILE", str(DEFAULT_OPERATORS)))),
        scenarios=scenarios,
    )
    config = uvicorn.Config(app, host="0.0.0.0", port=int(env("HTTP_PORT", "8090")), log_level="info")
    await uvicorn.Server(config).serve()


async def run_sim_cluster() -> None:
    import uvicorn

    from aiops_platform.sim.remote import create_cluster_app

    app = create_cluster_app(seconds_per_minute=float(env("SIM_SECONDS_PER_MINUTE", "10")))
    config = uvicorn.Config(app, host="0.0.0.0", port=int(env("HTTP_PORT", "8070")), log_level="warning")
    await uvicorn.Server(config).serve()


ROLES = {
    "context": run_context,
    "hitl": run_hitl,
    "orchestrator": run_orchestrator,
    "dashboard": run_dashboard,
    "sim-cluster": run_sim_cluster,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    if not args or (args[0] not in ROLES and args[0] != "worker"):
        print(__doc__)
        return 2
    if args[0] == "worker":
        if len(args) < 2:
            print(
                "usage: worker <monitoring|diagnosis|security-quality|deployment-recovery|resource-optimization|mlops-lifecycle>"
            )
            return 2
        asyncio.run(run_worker(args[1]))
    else:
        asyncio.run(ROLES[args[0]]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
