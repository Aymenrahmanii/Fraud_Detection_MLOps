"""Record a baseline of the platform: every benchmark number in one reproducible file.

    python -m aiops_platform.experiments.baseline --name deterministic-v1

Writes `baselines/<name>.json` (raw results) and `baselines/<name>.md` (the report).
Run it before a change you want to compare against, and again after.

Scenario, detection and sweep numbers are deterministic. The benchmark's
concurrent message handling is scheduled by the event loop, so its counts vary
a little from run to run (the 15%-fault row has moved between 70 and 74
resolved with the same code). Latencies depend on the machine and are recorded
with it.
"""

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import platform as pyplatform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from aiops_platform.demo import run_auto
from aiops_platform.experiments import benchmark, policy_sweep
from aiops_platform.policy.gate import load_policy
from aiops_platform.runtime import Platform
from aiops_platform.sim.scenarios import CHECKOUT, PAYMENT, base_cluster

OUT = Path(__file__).with_name("baselines")

# (label, incidents, duplicate rate, Context failure rate)
BENCHMARK_CASES = [
    ("clean-20", 20, 0.0, 0.0),
    ("faults-20", 20, 0.3, 0.05),
    ("clean-100", 100, 0.0, 0.0),
    ("faults-100-5pct", 100, 0.3, 0.05),
    ("faults-100-15pct", 100, 0.3, 0.15),
]


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def test_summary() -> str:
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "aiops_platform", "-q", "-p", "no:cacheprovider", "-W", "ignore"],
        capture_output=True,
        text=True,
    )
    lines = [line for line in run.stdout.strip().splitlines() if line.strip()]
    return lines[-1] if lines else run.stderr.strip()[-200:]


def summarize_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    rel = report["reliability"]
    d2d = report["latency_seconds"]["detect_to_decision"]
    return {
        "resolved": rel["resolved"],
        "by_stage": rel["by_stage"],
        "root_cause_accuracy": rel["root_cause_accuracy"],
        "one_change_per_resolved_incident": rel["exactly_one_change_per_resolved_incident"],
        "audit_chains_valid": rel["audit_chains_valid"],
        "published_messages": rel["published_records"],
        "injected_duplicates": rel["injected_duplicates"],
        "duplicates_ignored": rel["duplicates_ignored"],
        "injected_context_failures": rel["injected_context_failures"],
        "retries": rel["retries"],
        "dead_letters": rel["dead_letters"],
        "message_reliability": rel["message_reliability"],
        "detect_to_decision_p50_s": d2d["p50"],
        "detect_to_decision_p95_s": d2d["p95"],
        "throughput_per_min": report["throughput"]["incidents_resolved_per_minute"],
        "wall_seconds": report["throughput"]["wall_seconds"],
    }


async def detection_boundaries(tmp: Path) -> dict[str, Any]:
    base = load_policy()
    cpu = {}
    for rps in (450, 500, 550, 600, 640):
        cpu[rps] = (await policy_sweep.run_one(base, scenario="cpu", severity=rps, workdir=tmp / f"det-cpu-{rps}"))[
            "opened"
        ]
    leak = {}
    for minutes in (4, 5, 6, 7, 8, 9):
        leak[minutes] = (
            await policy_sweep.run_one(base, scenario="leak", severity=minutes, workdir=tmp / f"det-leak-{minutes}")
        )["opened"]
    return {"cpu_rps_incidents_opened": cpu, "leak_minutes_incidents_opened": leak}


async def healthy_control(tmp: Path, minutes: int = 20) -> dict[str, Any]:
    cluster = base_cluster()
    platform = await Platform.in_memory(tmp / "healthy", cluster, watched=[PAYMENT, CHECKOUT])
    opened = 0
    try:
        for _ in range(minutes):
            cluster.advance(1)
            opened += len(await platform.watcher.tick())
        await platform.bus.drain(timeout=60)
    finally:
        await platform.close()
    return {"minutes": minutes, "incidents_opened": opened}


def render(data: dict[str, Any]) -> str:
    meta = data["meta"]
    lines = [
        f"# Baseline: {meta['name']}",
        "",
        f"Recorded {meta['recorded_at']} at commit `{meta['commit']}`.",
        f"Machine: {meta['machine']}. Python {meta['python']}.",
        "",
        "Re-create with `python -m aiops_platform.experiments.baseline --name <name>`.",
        "",
        "## Tests",
        "",
        f"`pytest aiops_platform`: {data['tests']}",
        "",
        "## Scenarios through the full loop",
        "",
        "| Environment | Workflows | Incidents | Human approval rate | Autonomous resolution | Action success | False actions | Mean MTTR (s) | Mean MTTA (s) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for env in ("production", "staging"):
        s = data["scenarios"][env]
        lines.append(
            f"| {env} | {s['workflows']} | {s['incidents']} | {s['hitl_rate']} | "
            f"{s['autonomous_resolution_rate']} | {s['action_success_rate']} | {s['false_action_rate']} | "
            f"{s['mttr_s_avg']} | {s['mtta_s_avg']} |"
        )
    lines += [
        "",
        "Policy decisions: "
        + json.dumps(data["scenarios"]["production"]["policy_distribution"])
        + " (production), "
        + json.dumps(data["scenarios"]["staging"]["policy_distribution"])
        + " (staging).",
        "",
    ]
    lines += [
        "## Reliability under injected faults (benchmark, 100 or 20 concurrent incidents)",
        "",
        "| Run | Resolved | Escalated | One change per resolved incident | Audit valid | RCA accuracy | Duplicates injected / ignored | Context failures / retries | Dead letters | Detect→decision p50 / p95 (s) | Throughput (/min) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, b in data["benchmarks"].items():
        escalated = sum(v for k, v in b["by_stage"].items() if k == "ESCALATED")
        lines.append(
            f"| {label} | {b['resolved']} | {escalated} | {b['one_change_per_resolved_incident']} | "
            f"{b['audit_chains_valid']} | {b['root_cause_accuracy']} | "
            f"{b['injected_duplicates']} / {b['duplicates_ignored']} | "
            f"{b['injected_context_failures']} / {b['retries']} | {b['dead_letters']} | "
            f"{b['detect_to_decision_p50_s']} / {b['detect_to_decision_p95_s']} | {b['throughput_per_min']} |"
        )
    det = data["detection"]
    lines += [
        "",
        "## Detection boundaries (Agent 1, live path)",
        "",
        "Incidents opened by severity (1 run each):",
        "",
        f"- CPU load, requests/s: {det['cpu_rps_incidents_opened']}",
        f"- Memory leak, minutes of growth: {det['leak_minutes_incidents_opened']}",
        "",
        f"Healthy cluster, {data['healthy_control']['minutes']} minutes of normal traffic: "
        f"{data['healthy_control']['incidents_opened']} incidents opened (false alarms).",
        "",
        "## Policy sensitivity (±10% on every weight and threshold, 576 situations)",
        "",
        "| Parameter | Change | Decisions flipped | Rate | AUTO before → after |",
        "|---|---|---|---|---|",
    ]
    sens = data["sensitivity"]
    for row in sorted(sens["parameters"], key=lambda r: -r["flip_rate"])[:8]:
        lines.append(
            f"| {row['parameter']} | {row['change']} | {row['flipped']} | {row['flip_rate']:.1%} | "
            f"{row['auto_before']} → {row['auto_after']} |"
        )
    lines += ["", f"Baseline decisions: {json.dumps(sens['baseline_distribution'])}", ""]
    sweep = data["sweep"]["rows"]
    lines += [
        "## Threshold sweep on the loop",
        "",
        "| auto_decision_score | min_confidence | staging AUTO / opened | staging unsafe | production AUTO / opened | production unsafe | production approvals requested |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sweep:
        s, p = r["staging_cpu"], r["production_leak"]
        lines.append(
            f"| {r['auto_decision_score']} | {r['min_confidence']} | {s['auto']} / {s['opened']} | {s['unsafe']} | "
            f"{p['auto']} / {p['opened']} | {p['unsafe']} | {p['asked']} |"
        )
    lines += [
        "",
        "## Known limits of this baseline",
        "",
        "- Every number comes from the simulated cluster and its fault scenarios, on one machine.",
        "- Latencies depend on the machine and on the in-memory bus; the Docker stack was not measured here.",
        "- Historical success is a constant (0.5). Decision weights and thresholds are not calibrated against real outcomes.",
        "- The benchmark's observation windows are collapsed to zero, so MTTR here is processing time only.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="baseline name, e.g. deterministic-v1")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

    started = time.time()
    data: dict[str, Any] = {
        "meta": {
            "name": args.name,
            "recorded_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "commit": git_commit(),
            "machine": f"{pyplatform.system()} {pyplatform.release()}, {os.cpu_count()} logical CPUs",
            "python": pyplatform.python_version(),
        }
    }
    data["tests"] = test_summary()
    with tempfile.TemporaryDirectory(prefix="baseline-", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        data["scenarios"] = asyncio.run(run_auto(tmp / "scenarios"))
        data["benchmarks"] = {
            label: summarize_benchmark(asyncio.run(benchmark.run(n, dup, ctx, 7, tmp / label)))
            for label, n, dup, ctx in BENCHMARK_CASES
        }
        data["detection"] = asyncio.run(detection_boundaries(tmp))
        data["healthy_control"] = asyncio.run(healthy_control(tmp))
    data["sensitivity"] = policy_sweep.sensitivity()
    data["sweep"] = asyncio.run(policy_sweep.sweep())
    data["meta"]["wall_seconds"] = round(time.time() - started, 1)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (OUT / f"{args.name}.md").write_text(render(data), encoding="utf-8")
    print(f"wrote {OUT / (args.name + '.md')} ({data['meta']['wall_seconds']} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
