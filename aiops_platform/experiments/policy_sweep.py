"""How much do the policy parameters matter? Two experiments.

1. Sensitivity (pure, fast): over a grid of situations (action, environment, RCA
   confidence, evidence strength, agent agreement), perturb each weight and
   threshold of policy.yaml by +/-10% and count how often the decision flips.

2. Sweep (the real loop, in-process): run the staging CPU spike and the
   production memory leak at several severities, for a grid of the two
   thresholds that can change an automatic decision, and check the outcomes.

    python -m aiops_platform.experiments.policy_sweep
    python -m aiops_platform.experiments.policy_sweep --skip-sweep

The grid is over simulated scenarios. It shows which parameters the outcomes
depend on, not which values are optimal for real incidents: that needs
labelled real incidents.
"""

import argparse
import asyncio
import copy
import itertools
import json
import logging
import tempfile
from pathlib import Path

from aiops_platform.contracts.enums import Environment
from aiops_platform.hitl.service import Operator
from aiops_platform.policy import PolicyGate, PolicyInput
from aiops_platform.policy.gate import load_policy
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT, base_cluster, cpu_load_spike, memory_leak_after_deploy

SRE = Operator(subject="sweep@sre", roles=frozenset({"sre"}))

# Situations the gate actually sees: what Agents 2 and 5 propose, in each environment.
ACTIONS = [
    ("SCALE_REPLICAS", {"from": 3, "to": 5, "replicas": 5}),
    ("ROLLBACK", {"service": "payment-service"}),
    ("DEPLOY", {}),
]
RCA = [0.5, 0.6, 0.7, 0.8, 0.9, 0.98]
EVIDENCE = [0.5, 0.67, 0.8, 0.9]
AGREEMENT = [0.3, 0.75, 0.8, 1.0]

# Every weight and threshold, each perturbed by -10% and +10%.
PARAMETERS = [
    ("decision_weights", "rca_confidence"),
    ("decision_weights", "evidence_strength"),
    ("decision_weights", "historical_success"),
    ("decision_weights", "agent_agreement"),
    ("risk_weights", "environment_criticality"),
    ("risk_weights", "blast_radius"),
    ("risk_weights", "irreversibility"),
    ("risk_weights", "data_impact"),
    ("risk_weights", "uncertainty"),
    ("thresholds", "auto_decision_score"),
    ("thresholds", "min_confidence"),
    ("thresholds", "max_disagreement"),
    ("thresholds", "risk_levels", "LOW"),
    ("thresholds", "risk_levels", "MEDIUM"),
    ("thresholds", "risk_levels", "HIGH"),
]

# The sweep: the two thresholds that can turn an approval into an automatic action.
SWEEP_AUTO = [0.75, 0.80, 0.85, 0.90]
SWEEP_CONFIDENCE = [0.50, 0.60, 0.70]
CPU_RPS = [450, 640, 800]
LEAK_MINUTES = [6, 9, 12]


def situations():
    for (action, params), env, rca, evidence, agreement in itertools.product(
        ACTIONS, ["staging", "production"], RCA, EVIDENCE, AGREEMENT
    ):
        yield action, env, params, rca, evidence, agreement


def decide(policy: dict, situation) -> str:
    action, env, params, rca, evidence, agreement = situation
    x = PolicyInput(
        action=action,
        environment=env,
        parameters=params,
        rca_confidence=rca,
        evidence_strength=evidence,
        agent_agreement=agreement,
    )
    return PolicyGate(policy).evaluate(x).decision.value


def scaled(policy: dict, path: tuple, factor: float) -> dict:
    changed = copy.deepcopy(policy)
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = round(node[path[-1]] * factor, 4)
    return changed


def sensitivity() -> dict:
    base = load_policy()
    cases = list(situations())
    baseline = [decide(base, s) for s in cases]
    rows = []
    for path in PARAMETERS:
        for factor in (0.9, 1.1):
            changed = scaled(base, path, factor)
            decisions = [decide(changed, s) for s in cases]
            flips = sum(a != b for a, b in zip(baseline, decisions, strict=True))
            rows.append(
                {
                    "parameter": ".".join(path),
                    "change": f"{factor - 1:+.0%}",
                    "flipped": flips,
                    "flip_rate": round(flips / len(cases), 4),
                    "auto_before": baseline.count("AUTO"),
                    "auto_after": decisions.count("AUTO"),
                }
            )
    distribution = {
        env: {
            d: sum(1 for s, b in zip(cases, baseline, strict=True) if s[1] == env and b == d)
            for d in ("AUTO", "APPROVAL_REQUIRED", "BLOCK")
        }
        for env in ("staging", "production")
    }
    return {"situations": len(cases), "baseline_distribution": distribution, "parameters": rows}


async def run_one(policy: dict, *, scenario: str, severity, workdir: Path) -> dict:
    environment = Environment.STAGING if scenario == "cpu" else Environment.PRODUCTION
    cluster = base_cluster()
    watched = [CHECKOUT] if scenario == "cpu" else [PAYMENT, CHECKOUT]
    platform = await Platform.in_memory(
        workdir, cluster, watched=watched, environment=environment, policy=PolicyGate(policy)
    )
    try:
        if scenario == "cpu":
            cpu_load_spike(cluster, rps=severity)
        else:
            memory_leak_after_deploy(cluster, minutes=severity)
        opened = await platform.watcher.tick()
        await platform.bus.drain(timeout=120)
        asked = 0
        for approval in await platform.pending_approvals():
            asked += 1
            await platform.orchestrator.resolve_approval(
                approval["approval_id"], decision="APPROVE", operator=SRE, comment="policy sweep"
            )
            await platform.bus.drain(timeout=120)
        result = {"opened": len(opened), "asked": asked, "auto": 0, "resolved": 0, "unsafe": 0}
        for incident in opened:
            status = await platform.orchestrator.status(incident)
            decision = (status.get("policy") or {}).get("decision")
            result["auto"] += decision == "AUTO"
            result["resolved"] += status.get("stage") == "RESOLVED"
        # An unsafe run changed the cluster but did not end resolved.
        result["unsafe"] = int(len(cluster.mutations) > 0 and result["resolved"] < result["opened"])
        return result
    finally:
        await platform.close()


async def sweep() -> dict:
    base = load_policy()
    rows = []
    # Windows keeps the SQLite journal open briefly after close(); a leftover temp folder is harmless.
    with tempfile.TemporaryDirectory(prefix="policy-sweep-", ignore_cleanup_errors=True) as tmp:
        for auto, confidence in itertools.product(SWEEP_AUTO, SWEEP_CONFIDENCE):
            policy = copy.deepcopy(base)
            policy["thresholds"]["auto_decision_score"] = auto
            policy["thresholds"]["min_confidence"] = confidence
            row = {
                "auto_decision_score": auto,
                "min_confidence": confidence,
                "staging_cpu": {"opened": 0, "auto": 0, "resolved": 0, "unsafe": 0, "asked": 0},
                "production_leak": {"opened": 0, "auto": 0, "resolved": 0, "unsafe": 0, "asked": 0},
            }
            for scenario, severities in (("cpu", CPU_RPS), ("leak", LEAK_MINUTES)):
                key = "staging_cpu" if scenario == "cpu" else "production_leak"
                for i, severity in enumerate(severities):
                    workdir = Path(tmp) / f"{auto}-{confidence}-{scenario}-{i}"
                    result = await run_one(policy, scenario=scenario, severity=severity, workdir=workdir)
                    for field, value in result.items():
                        row[key][field] += value
            rows.append(row)
    return {
        "grid": {
            "auto_decision_score": SWEEP_AUTO,
            "min_confidence": SWEEP_CONFIDENCE,
            "cpu_rps": CPU_RPS,
            "leak_minutes": LEAK_MINUTES,
        },
        "rows": rows,
    }


def print_sensitivity(report: dict) -> None:
    print(f"\nSensitivity over {report['situations']} situations")
    print("Baseline decisions:", json.dumps(report["baseline_distribution"]))
    print(f"{'parameter':42} {'change':>6} {'flipped':>8} {'rate':>7} {'AUTO':>12}")
    for row in sorted(report["parameters"], key=lambda r: -r["flip_rate"]):
        print(
            f"{row['parameter']:42} {row['change']:>6} {row['flipped']:>8} {row['flip_rate']:>7.1%} "
            f"{row['auto_before']:>5} -> {row['auto_after']:<5}"
        )


def print_sweep(report: dict) -> None:
    print("\nSweep on the real loop (staging CPU x3, production leak x3 per row)")
    print(
        f"{'auto':>5} {'conf':>5} | {'staging: opened auto resolved unsafe asked':>44} | "
        f"{'production: opened auto resolved unsafe asked':>46}"
    )
    for r in report["rows"]:
        s, p = r["staging_cpu"], r["production_leak"]
        print(
            f"{r['auto_decision_score']:>5.2f} {r['min_confidence']:>5.2f} | "
            f"{s['opened']:>14}{s['auto']:>6}{s['resolved']:>9}{s['unsafe']:>8}{s['asked']:>7}   | "
            f"{p['opened']:>13}{p['auto']:>6}{p['resolved']:>9}{p['unsafe']:>8}{p['asked']:>7}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-sweep", action="store_true", help="only the fast sensitivity analysis")
    parser.add_argument("--json", type=Path, help="write the full results here")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    report = {"sensitivity": sensitivity()}
    print_sensitivity(report["sensitivity"])
    if not args.skip_sweep:
        report["sweep"] = asyncio.run(sweep())
        print_sweep(report["sweep"])
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
