"""Hard cases: incidents that look like one thing and are another. Ground truth is fixed in advance.

Each family has a symptom that a fixed rule can mislead:

- leak: memory climbs after a deploy. Truth: roll back. (Control for the other families.)
- cpu_burn: a release burns CPU at any traffic level. Truth: roll back. A scale-up
  looks like the obvious fix and only hides the release problem.
- load_memory: a traffic spike pushes memory and CPU up after a deploy. Truth: scale.
  A rollback looks plausible because a deploy came first, and does nothing.
- traffic: a traffic spike on a service with no recent deploy. Truth: scale.
- control: nothing happens. Truth: no incident.

The operator is worst-case: every approval is granted. The report therefore shows
what would have run in the real world if the operator trusted the proposal.

    python -m aiops_platform.experiments.ambiguous --name ambiguous-rules-v1
"""

import argparse
import asyncio
import json
import logging
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_platform.hitl.service import Operator
from aiops_platform.runtime import Platform
from aiops_platform.sim.cluster import Release, SimulatedCluster, digest_image
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT

SRE = Operator(subject="ambiguous@sre", roles=frozenset({"sre"}))
OUT = Path(__file__).with_name("baselines")


def service_cluster(payment_fault: str | None) -> SimulatedCluster:
    """payment-service running v2.6, with a v2.7 build that carries `payment_fault`; checkout-api healthy."""
    cluster = SimulatedCluster()
    repo = "registry.local/payment-service"
    faults = frozenset({payment_fault}) if payment_fault else frozenset()
    cluster.add_service(
        "payment-service",
        "prod",
        [Release("v2.6", digest_image(repo, "v2.6")), Release("v2.7", digest_image(repo, "v2.7"), faults)],
        current="v2.6",
        replicas=4,
    )
    repo = "registry.local/checkout-api"
    cluster.add_service(
        "checkout-api",
        "prod",
        [Release("v1.3", digest_image(repo, "v1.3")), Release("v1.4", digest_image(repo, "v1.4"))],
        current="v1.4",
        replicas=3,
        rps=250,
    )
    return cluster


@dataclass(frozen=True)
class Case:
    family: str
    variant: str
    truth: str | None  # the one action that is right, or None for "nothing to do"
    payment_fault: str | None
    inject: Callable[[SimulatedCluster], None]


def _deploy_v27(cluster: SimulatedCluster) -> None:
    cluster.release_version("payment-service", "prod", "v2.7")


def cases() -> list[Case]:
    out: list[Case] = []
    for minutes in (8, 9, 10, 11):
        out.append(
            Case(
                "leak",
                f"{minutes} min after deploy",
                "ROLLBACK",
                "memory_leak",
                lambda c, m=minutes: (_deploy_v27(c), c.advance(m)),
            )
        )
    for rps in (260, 300, 340, 380):
        out.append(
            Case(
                "cpu_burn",
                f"{rps} rps",
                "ROLLBACK",
                "cpu_burn",
                lambda c, r=rps: (_deploy_v27(c), c.set_load("payment-service", "prod", r), c.advance(9)),
            )
        )
    for rps in (680, 720, 760, 800):
        out.append(
            Case(
                "load_memory",
                f"{rps} rps after deploy",
                "SCALE_REPLICAS",
                "load_memory",
                lambda c, r=rps: (_deploy_v27(c), c.set_load("payment-service", "prod", r), c.advance(9)),
            )
        )
    for rps in (600, 640, 680, 720):
        out.append(
            Case(
                "traffic",
                f"{rps} rps, no deploy",
                "SCALE_REPLICAS",
                None,
                lambda c, r=rps: (c.set_load("checkout-api", "prod", r), c.advance(9)),
            )
        )
    out.append(Case("control", "steady traffic, 9 min", None, None, lambda c: c.advance(9)))
    out.append(Case("control", "healthy deploy, 9 min", None, None, lambda c: (_deploy_v27(c), c.advance(9))))
    return out


def snapshot(cluster: SimulatedCluster) -> dict[str, tuple[str, int, str]]:
    return {
        name: (dep.image, dep.replicas, dep.version)
        for name, dep in ((s, cluster.deployment(s, "prod")) for s in ("payment-service", "checkout-api"))
    }


def executed(before: dict, after: dict) -> list[str]:
    actions = []
    for name, (image, replicas, version) in after.items():
        old_image, old_replicas, old_version = before[name]
        if image != old_image:
            actions.append("ROLLBACK" if _is_older(version, old_version) else "DEPLOY")
        if replicas != old_replicas:
            actions.append("SCALE_REPLICAS")
    return actions


def _is_older(version: str, than: str) -> bool:
    return version < than  # v2.6 < v2.7, the only release order used here


WRONG_PROPOSAL_TYPES = {"ROLLBACK", "SCALE_REPLICAS"}


def verdict(case: Case, opened: bool, stage: str | None, proposed: str | None, run: list[str]) -> str:
    if case.truth is None:
        return "quiet (correct)" if not opened else "false alarm"
    if not opened:
        return "missed (no incident)"
    if any(action != case.truth for action in run):
        # The platform's own verdict, when it says RESOLVED after a wrong change, is a false recovery.
        if stage == "RESOLVED":
            return "WRONG change executed, reported RESOLVED (false recovery)"
        return "WRONG change executed"
    if case.truth in run:
        return "correct"
    if proposed in WRONG_PROPOSAL_TYPES and proposed != case.truth:
        return "wrong action shown to a human (nothing executed)"
    if stage == "ESCALATED":
        return "escalated to a human (nothing executed)"
    return f"no change (stage {stage})"


async def run_case(case: Case, *, investigator, workdir: Path) -> dict[str, Any]:
    cluster = service_cluster(case.payment_fault)
    platform = await Platform.in_memory(workdir, cluster, watched=[PAYMENT, CHECKOUT], agent2_investigator=investigator)
    try:
        case.inject(cluster)
        opened = await platform.watcher.tick()
        await platform.bus.drain(timeout=120)
        before = snapshot(cluster)
        proposed = stage = None
        for incident in opened[:1]:
            status = await platform.orchestrator.status(incident)
            proposed = (status.get("selected") or {}).get("type")
            stage = status.get("stage")
        approvals = await platform.pending_approvals()
        for approval in approvals:
            await platform.orchestrator.resolve_approval(
                approval["approval_id"], decision="APPROVE", operator=SRE, comment="worst-case operator"
            )
            await platform.bus.drain(timeout=120)
        if opened:
            stage = (await platform.orchestrator.status(opened[0])).get("stage")
        run = executed(before, snapshot(cluster))
        return {
            "family": case.family,
            "variant": case.variant,
            "truth": case.truth,
            "opened": len(opened),
            "proposed": proposed,
            "approvals_asked": len(approvals),
            "executed": run,
            "stage": stage,
            "outcome": verdict(case, bool(opened), stage, proposed, run),
        }
    finally:
        await platform.close()


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(row["family"], {})
        bucket[row["outcome"]] = bucket.get(row["outcome"], 0) + 1
    return summary


def render(name: str, rows: list[dict[str, Any]], summary: dict[str, dict[str, int]], recorded: str) -> str:
    lines = [
        f"# Hard cases: {name}",
        "",
        f"Recorded {recorded}. Deterministic rules only unless the name says otherwise.",
        "Ground truth is fixed in advance. The operator approves every request (worst case).",
        "",
        "## Outcomes by family",
        "",
        "| Family | Outcomes |",
        "|---|---|",
    ]
    for family, outcomes in summary.items():
        text = "; ".join(f"{k}: {v}" for k, v in outcomes.items())
        lines.append(f"| {family} | {text} |")
    lines += [
        "",
        "## Cases",
        "",
        "| Family | Variant | Truth | Proposed | Executed | Outcome |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['family']} | {r['variant']} | {r['truth'] or '—'} | {r['proposed'] or '—'} | "
            f"{', '.join(r['executed']) or '—'} | {r['outcome']} |"
        )
    lines.append("")
    return "\n".join(lines)


async def run_all(*, investigator_factory=None) -> list[dict[str, Any]]:
    rows = []
    with tempfile.TemporaryDirectory(prefix="ambiguous-", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        for i, case in enumerate(cases()):
            investigator = None if investigator_factory is None else investigator_factory
            rows.append(await run_case(case, investigator=investigator, workdir=tmp / f"case-{i}"))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    started = time.time()
    rows = asyncio.run(run_all())
    summary = summarize(rows)
    recorded = time.strftime("%Y-%m-%d %H:%M")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.name}.json").write_text(json.dumps({"rows": rows, "summary": summary}, indent=2), encoding="utf-8")
    (OUT / f"{args.name}.md").write_text(render(args.name, rows, summary, recorded), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"wrote {OUT / (args.name + '.md')} in {round(time.time() - started, 1)} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
