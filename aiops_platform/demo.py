"""Offline demonstration of the closed loop (no Docker, no cluster, no API keys).

    python -m aiops_platform.demo --auto     scripted run of the v5 scenarios + metrics
    python -m aiops_platform.demo serve      platform + Dashboard on http://127.0.0.1:8090

Everything runs in one process on the in-memory bus and the simulated cluster;
the agents, Orchestrator, policy gate, HITL Service and Agent 4's coordinator
are the real code.
"""

import argparse
import asyncio
import json
import logging
import os
import tempfile
import time
import warnings
from pathlib import Path

from aiops_platform.context.service import Principal
from aiops_platform.contracts.enums import Environment
from aiops_platform.experiments.metrics import evaluate_workflows
from aiops_platform.hitl.service import Operator
from aiops_platform.orchestrator.graph import approval_ttl_seconds
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT, base_cluster, cpu_load_spike, memory_leak_after_deploy

SRE = Operator(subject="alice@sre", roles=frozenset({"sre"}))
RELEASE = Operator(subject="rita@release", roles=frozenset({"release-manager"}))
ML = Operator(subject="max@ml", roles=frozenset({"ml-lead"}))


def say(text: str) -> None:
    print(text, flush=True)


async def approve_all(platform: Platform, operator: Operator, comment: str) -> list[str]:
    done = []
    for approval in await platform.pending_approvals():
        req = approval["request_json"]
        say(
            f"   HITL  {operator.subject} approves {req['action']} on {approval['incident_id']} "
            f"(risk {req['risk'].get('level')}, triggers: {'; '.join(req.get('hitl_triggers', [])[:2])})"
        )
        await platform.orchestrator.resolve_approval(
            approval["approval_id"], decision="APPROVE", operator=operator, comment=comment
        )
        await platform.bus.drain(timeout=120)
        done.append(approval["incident_id"])
    return done


async def show(platform: Platform, incident_id: str) -> None:
    st = await platform.orchestrator.status(incident_id)
    sel = st.get("selected") or {}
    pol = st.get("policy") or {}
    say(
        f"   -> {incident_id}: stage={st.get('stage')} outcome={st.get('outcome')} "
        f"action={sel.get('type')} policy={pol.get('decision')} root_cause={st.get('consensus', {}).get('primary_root_cause')}"
    )


def release_assessment(fixed: bool, assessment_id: str, commit: str) -> dict:
    """Agent 3's assessment of payment-service v2.8: one failing test, or the fixed build."""
    return {
        "assessment_id": assessment_id,
        "commit": commit,
        "artifact": {"image": "registry.local/payment-service:v2.8"},
        "tests": {
            "total": 124,
            "passed": 124 if fixed else 123,
            "failed": 0 if fixed else 1,
            "skipped": 0,
            "coverage_pct": 87.3 if fixed else 86.0,
        },
        "quality": {"gate": "PASSED"},
        "security": {},
        "decision": "ALLOW" if fixed else "BLOCK",
        "blocking_reasons": [] if fixed else ["1 failing test"],
    }


async def run_auto(workdir: Path) -> dict:
    prod = await Platform.in_memory(workdir / "prod", base_cluster(), watched=[PAYMENT, CHECKOUT])
    staging = await Platform.in_memory(
        workdir / "staging", base_cluster(), watched=[CHECKOUT], environment=Environment.STAGING
    )
    try:
        say("\n[1] Memory leak introduced by release v2.7 (production)")
        memory_leak_after_deploy(prod.cluster)
        say(
            f"   cluster: payment-service {prod.cluster.deployment('payment-service', 'prod').version}, "
            f"memory {prod.cluster.signals('payment-service')['memory_pct']}%"
        )
        [incident] = await prod.watcher.tick()
        await prod.bus.drain(timeout=120)
        await show(prod, incident)
        await approve_all(prod, SRE, "Rollback approved; verify recovery.")
        await show(prod, incident)
        say(
            f"   cluster: payment-service {prod.cluster.deployment('payment-service', 'prod').version}, "
            f"memory {prod.cluster.signals('payment-service')['memory_pct']}%"
        )

        say("\n[2] CPU saturation from a load spike (staging: bounded autonomy)")
        cpu_load_spike(staging.cluster)
        [incident2] = await staging.watcher.tick()
        await staging.bus.drain(timeout=120)
        await show(staging, incident2)
        say(f"   cluster: checkout-api replicas {staging.cluster.deployment('checkout-api', 'prod').replicas}")

        say("\n[3] Release v2.8 with a failing test (Agent 3 gate)")
        blocked = release_assessment(False, "REL-DEMO-1", "c0ffee1")
        await prod.orchestrator.start_release(
            release_id="REL-payment-v2.8-a",
            service={"service_id": "payment-service", "namespace": "prod", "version": "v2.6"},
            environment="production",
            version="v2.8",
            commit="c0ffee1",
            assessment=blocked,
        )
        await prod.bus.drain(timeout=120)
        await show(prod, "REL-payment-v2.8-a")

        say("\n[4] Release v2.8 after the fix (Agent 3 ALLOW -> deploy with approval -> verify)")
        allowed = release_assessment(True, "REL-DEMO-2", "c0ffee2")
        await prod.orchestrator.start_release(
            release_id="REL-payment-v2.8-b",
            service={"service_id": "payment-service", "namespace": "prod", "version": "v2.6"},
            environment="production",
            version="v2.8",
            commit="c0ffee2",
            assessment=allowed,
        )
        await prod.bus.drain(timeout=120)
        await approve_all(prod, RELEASE, "Ship v2.8.")
        await show(prod, "REL-payment-v2.8-b")

        say("\n[5] Model drift: assess -> retrain -> champion/challenger -> human promotion review")
        await prod.orchestrator.start_model_check(
            check_id="MLOPS-machine-failure-demo",
            service={"service_id": "machine-failure", "namespace": "ml", "version": "v1"},
            environment="production",
        )
        await prod.bus.drain(timeout=300)
        st = await prod.orchestrator.status("MLOPS-machine-failure-demo")
        health = st["results"]["model_health"]["payload"]
        lifecycle = st["results"].get("lifecycle", {}).get("payload", {})
        say(
            f"   Agent 6: drift={health['drift_detected']} (score {health['drift_score']}), decision {health['decision']}"
        )
        if lifecycle:
            say(
                f"   candidate F1 {lifecycle.get('candidate', {}).get('f1')} vs champion F1 {lifecycle.get('champion', {}).get('f1')} -> {lifecycle['decision']}"
            )
        await approve_all(prod, ML, "Promote after canary review.")
        await show(prod, "MLOPS-machine-failure-demo")

        report = {
            "production": await evaluate_workflows(prod),
            "staging": await evaluate_workflows(staging),
        }
        say("\nEvaluation (v5 section 18 metrics)")
        say(json.dumps(report, indent=2))
        return report
    finally:
        await prod.close()
        await staging.close()


async def serve(workdir: Path, port: int) -> None:
    import uvicorn

    from aiops_platform.dashboard.app import create_app

    platform = await Platform.in_memory(workdir, base_cluster(), watched=[PAYMENT, CHECKOUT])

    async def inject_leak():
        memory_leak_after_deploy(platform.cluster)
        opened = await platform.watcher.tick()
        return opened

    async def inject_cpu():
        cpu_load_spike(platform.cluster)
        return await platform.watcher.tick()

    async def model_check():
        incident = f"MLOPS-machine-failure-{int(time.time())}"
        await platform.orchestrator.start_model_check(
            check_id=incident,
            service={"service_id": "machine-failure", "namespace": "ml", "version": "v1"},
            environment="production",
        )
        return [incident]

    async def release(fixed: bool) -> list[str]:
        """payment-service v2.8 through Agent 3's gate: blocked by a failing test, or allowed."""
        stamp = int(time.time())
        release_id = f"REL-payment-v2.8-{'fixed' if fixed else 'failing-test'}-{stamp}"
        commit = "c0ffee2" if fixed else "c0ffee1"
        current = platform.cluster.deployment(PAYMENT.service_id, PAYMENT.namespace).version
        await platform.orchestrator.start_release(
            release_id=release_id,
            service={"service_id": PAYMENT.service_id, "namespace": PAYMENT.namespace, "version": current},
            environment="production",
            version="v2.8",
            commit=commit,
            assessment=release_assessment(fixed, f"REL-DEMO-{stamp}", commit),
        )
        return [release_id]

    async def release_failing_test():
        return await release(False)

    async def release_fixed():
        return await release(True)

    app = create_app(
        context=platform.context,
        orchestrator=platform.orchestrator,
        metrics_registry=platform.orchestrator.metrics.registry,
        scenarios={
            "memory_leak_after_deploy": inject_leak,
            "cpu_load_spike": inject_cpu,
            "model_drift_check": model_check,
            "release_v28_failing_test": release_failing_test,
            "release_v28_fixed": release_fixed,
        },
    )

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            await platform.orchestrator.sweep()
            for incident in await platform.context.list_incidents(Principal.orchestrator()):
                if incident.closed_at and incident.service:
                    platform.watcher.resolve(incident.service.service_id)

    task = asyncio.create_task(heartbeat())
    say(
        f"Dashboard: http://127.0.0.1:{port}  (tokens: dev-sre-token, dev-release-token, dev-ml-token, dev-viewer-token)"
    )
    say(f"Approvals expire after {approval_ttl_seconds()} s (--approval-ttl to change)")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    try:
        await uvicorn.Server(config).serve()
    finally:
        task.cancel()
        await platform.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="auto", choices=["auto", "serve"])
    parser.add_argument("--auto", action="store_true", help="same as mode=auto")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--workdir", type=Path, help="state directory (default: a temporary directory)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--approval-ttl",
        type=int,
        metavar="SECONDS",
        help="how long a human has to decide (default 600, at least 30); expired approvals are re-requested once",
    )
    args = parser.parse_args(argv)
    if args.approval_ttl:
        os.environ["AIOPS_APPROVAL_TTL_SECONDS"] = str(args.approval_ttl)
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    # Third-party training chatter (sklearn solver options, MLflow schema hints) is not demo output.
    warnings.filterwarnings("ignore", module=r"(sklearn|mlflow)(\.|$)")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)
    for noisy in ("mlflow", "alembic", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="aiops-demo-"))
    if args.mode == "serve" and not args.auto:
        asyncio.run(serve(workdir, args.port))
    else:
        asyncio.run(run_auto(workdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
