# Baseline: deterministic-v1

Recorded 2026-11-08 10:43 UTC at commit `266bdd2`.
Machine: Windows 11, 16 logical CPUs. Python 3.12.2.

Re-create with `python -m aiops_platform.experiments.baseline --name <name>`.

## Tests

`pytest aiops_platform`: 44 passed in 39.62s

## Scenarios through the full loop

| Environment | Workflows | Incidents | Human approval rate | Autonomous resolution | Action success | False actions | Mean MTTR (s) | Mean MTTA (s) |
|---|---|---|---|---|---|---|---|---|
| production | 4 | 1 | 0.75 | 0.0 | 1.0 | 0.0 | 1.198 | 0.422 |
| staging | 1 | 1 | 0.0 | 1.0 | 1.0 | 0.0 | 1.007 | 0.349 |

Policy decisions: {"APPROVAL_REQUIRED": 2} (production), {"APPROVAL_REQUIRED": 1, "AUTO": 1} (staging).

## Reliability under injected faults (benchmark, 100 or 20 concurrent incidents)

| Run | Resolved | Escalated | One change per resolved incident | Audit valid | RCA accuracy | Duplicates injected / ignored | Context failures / retries | Dead letters | Detect→decision p50 / p95 (s) | Throughput (/min) |
|---|---|---|---|---|---|---|---|---|---|---|
| clean-20 | 20 | 0 | True | True | 1.0 | 0 / 0 | 0 / 0 | 0 | 5.3446 / 8.4857 | 64.81 |
| faults-20 | 20 | 0 | True | True | 1.0 | 95 / 81.0 | 20 / 20.0 | 0 | 5.4323 / 7.2391 | 71.17 |
| clean-100 | 100 | 0 | True | True | 1.0 | 0 / 0 | 0 / 0 | 0 | 22.5535 / 34.7402 | 64.58 |
| faults-100-5pct | 100 | 0 | True | True | 1.0 | 462 / 399.0 | 110 / 110.0 | 0 | 23.5409 / 35.2439 | 62.6 |
| faults-100-15pct | 74 | 26 | True | True | 1.0 | 497 / 455.0 | 484 / 458.0 | 26 | 26.0753 / 44.6677 | 48.81 |

## Detection boundaries (Agent 1, live path)

Incidents opened by severity (1 run each):

- CPU load, requests/s: {450: 0, 500: 0, 550: 1, 600: 1, 640: 1}
- Memory leak, minutes of growth: {4: 0, 5: 0, 6: 0, 7: 1, 8: 1, 9: 1}

Healthy cluster, 20 minutes of normal traffic: 0 incidents opened (false alarms).

## Policy sensitivity (±10% on every weight and threshold, 576 situations)

| Parameter | Change | Decisions flipped | Rate | AUTO before → after |
|---|---|---|---|---|
| thresholds.auto_decision_score | -10% | 57 | 9.9% | 3 → 60 |
| decision_weights.rca_confidence | +10% | 15 | 2.6% | 3 → 18 |
| decision_weights.evidence_strength | +10% | 12 | 2.1% | 3 → 15 |
| decision_weights.agent_agreement | +10% | 6 | 1.0% | 3 → 9 |
| decision_weights.rca_confidence | -10% | 3 | 0.5% | 3 → 0 |
| decision_weights.evidence_strength | -10% | 3 | 0.5% | 3 → 0 |
| decision_weights.historical_success | +10% | 3 | 0.5% | 3 → 6 |
| thresholds.auto_decision_score | +10% | 3 | 0.5% | 3 → 0 |

Baseline decisions: {"staging": {"AUTO": 3, "APPROVAL_REQUIRED": 285, "BLOCK": 0}, "production": {"AUTO": 0, "APPROVAL_REQUIRED": 288, "BLOCK": 0}}

## Threshold sweep on the loop

| auto_decision_score | min_confidence | staging AUTO / opened | staging unsafe | production AUTO / opened | production unsafe | production approvals requested |
|---|---|---|---|---|---|---|
| 0.75 | 0.5 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.75 | 0.6 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.75 | 0.7 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.8 | 0.5 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.8 | 0.6 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.8 | 0.7 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.85 | 0.5 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.85 | 0.6 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.85 | 0.7 | 2 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.9 | 0.5 | 0 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.9 | 0.6 | 0 / 2 | 0 | 0 / 2 | 0 | 2 |
| 0.9 | 0.7 | 0 / 2 | 0 | 0 / 2 | 0 | 2 |

## Known limits of this baseline

- Every number comes from the simulated cluster and its fault scenarios, on one machine.
- Latencies depend on the machine and on the in-memory bus; the Docker stack was not measured here.
- Historical success is a constant (0.5). Decision weights and thresholds are not calibrated against real outcomes.
- The benchmark's observation windows are collapsed to zero, so MTTR here is processing time only.
