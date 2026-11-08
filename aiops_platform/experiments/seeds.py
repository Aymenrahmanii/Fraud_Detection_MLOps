"""The benchmark over several seeds, reported as ranges instead of single runs.

    python -m aiops_platform.experiments.seeds --name benchmark-seeds-v1

Each configuration runs with every seed. The report gives the median and the
range (min to max) of each measure, so the run-to-run variation is visible
rather than hidden.
"""

import argparse
import asyncio
import json
import logging
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from aiops_platform.experiments import benchmark

OUT = Path(__file__).with_name("baselines")
SEEDS = [7, 11, 23, 42, 101]
CONFIGS = [
    ("clean-100", 100, 0.0, 0.0),
    ("faults-100-5pct", 100, 0.3, 0.05),
    ("faults-100-15pct", 100, 0.3, 0.15),
]
MEASURES = {
    "resolved": lambda r: r["reliability"]["resolved"],
    "escalated": lambda r: sum(v for k, v in r["reliability"]["by_stage"].items() if k == "ESCALATED"),
    "one_change_per_resolved": lambda r: int(r["reliability"]["exactly_one_change_per_resolved_incident"]),
    "audit_valid": lambda r: int(r["reliability"]["audit_chains_valid"]),
    "rca_accuracy": lambda r: r["reliability"]["root_cause_accuracy"],
    "dead_letters": lambda r: r["reliability"]["dead_letters"],
    "throughput_per_min": lambda r: r["throughput"]["incidents_resolved_per_minute"],
    "detect_to_decision_p50_s": lambda r: r["latency_seconds"]["detect_to_decision"]["p50"],
    "detect_to_decision_p95_s": lambda r: r["latency_seconds"]["detect_to_decision"]["p95"],
}


def summarize(values: list[float]) -> dict[str, float]:
    return {"median": round(statistics.median(values), 3), "min": round(min(values), 3), "max": round(max(values), 3)}


def render(name: str, recorded: str, results: dict[str, list[dict[str, Any]]]) -> str:
    lines = [
        f"# Benchmark over seeds: {name}",
        "",
        f"Recorded {recorded}. Seeds: {', '.join(map(str, SEEDS))}. Each cell is median (min to max) over the seeds.",
        "",
    ]
    for label, runs in results.items():
        lines += [f"## {label}", "", "| Measure | Median (min to max) |", "|---|---|"]
        for measure in MEASURES:
            s = summarize([run[measure] for run in runs])
            lines.append(f"| {measure} | {s['median']} ({s['min']} to {s['max']}) |")
        lines.append("")
    return "\n".join(lines)


async def run_all() -> dict[str, list[dict[str, Any]]]:
    results: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="seeds-", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        for label, n, dup, ctx in CONFIGS:
            runs = []
            for seed in SEEDS:
                report = await benchmark.run(n, dup, ctx, seed, tmp / f"{label}-{seed}")
                runs.append({m: float(fn(report)) for m, fn in MEASURES.items()})
            results[label] = runs
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    started = time.time()
    results = asyncio.run(run_all())
    recorded = time.strftime("%Y-%m-%d %H:%M")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.name}.json").write_text(json.dumps({"seeds": SEEDS, "runs": results}, indent=2), encoding="utf-8")
    (OUT / f"{args.name}.md").write_text(render(args.name, recorded, results), encoding="utf-8")
    print(f"wrote {OUT / (args.name + '.md')} in {round(time.time() - started, 1)} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
