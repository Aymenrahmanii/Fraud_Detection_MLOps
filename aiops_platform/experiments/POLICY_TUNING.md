# Policy parameters and stress test

What the decisions are based on, which parameters matter, and what the platform
was tested to. All numbers come from the commands below, on this code, on one
laptop. They describe the simulator, not production traffic.

    python -m aiops_platform.experiments.policy_sweep --json sweep.json
    python -m aiops_platform.experiments.benchmark -n 100 --duplicates 0.3 --context-failures 0.15

## How the decisions are weighted

No single agent votes. Each returns a typed output; the Orchestrator turns the
outputs into candidate actions and the policy gate decides.

**Candidate scores (consensus)**
- Agent 2 (diagnosis): the RCA probability of the leading hypothesis, halved for each lower-ranked recommendation.
- Agent 5 (resources): its confidence, halved when the root cause is one that more capacity cannot fix (memory leak, crash loop, pool exhaustion, dependency down).

**Agreement** (a fixed table, not learned): 1.0 when both agents propose compatible
actions; 0.8 when only one agent speaks; 0.75 when a containment action sits next
to a fix; 0.3 when the proposals are incompatible; capped at 0.45 when Agent 2
asks for more evidence.

**Decision score** = 0.40 × RCA confidence + 0.25 × evidence strength
+ 0.20 × historical success + 0.15 × agreement.

**Risk** = 0.30 × environment criticality + 0.20 × blast radius
+ 0.20 × irreversibility + 0.15 × data impact + 0.15 × uncertainty (1 − decision score).
Levels: LOW below 0.35, MEDIUM below 0.60, HIGH below 0.80, CRITICAL above.

**Outcome**: BLOCK for a forbidden action, a failed release gate or stale context;
AUTO only when risk is LOW, no trigger fires and the decision score is at least 0.85;
otherwise APPROVAL_REQUIRED.

Two consequences of these values:
- **Historical success is a constant 0.5.** No history is stored yet, so it adds a fixed 0.10 and the score cannot learn from past outcomes.
- **AUTO needs near-maximal inputs.** With evidence at 0.9 and full agreement, the RCA probability must reach about 0.94 to clear 0.85.

## Sensitivity: which parameters change decisions

576 situations (3 actions × 2 environments × 6 RCA × 4 evidence × 4 agreement
values), each parameter perturbed by ±10%:

| Parameter | Change | Decisions flipped | AUTO before → after |
|---|---|---|---|
| auto_decision_score | −10% | 57 (9.9%) | 3 → 60 |
| rca_confidence weight | +10% | 15 (2.6%) | 3 → 18 |
| evidence_strength weight | +10% | 12 (2.1%) | 3 → 15 |
| agent_agreement weight | +10% | 6 (1.0%) | 3 → 9 |
| every other weight and threshold | ±10% | 0–3 | |
| risk weights, min_confidence, max_disagreement, risk levels | ±10% | **0** | |

**Production never reaches AUTO** under any setting (288 of 288 approval).
Production is always at least MEDIUM risk (the environment alone adds 0.30) and always carries the
"production side effect without pre-authorization" trigger, so the policy
stops it structurally. Staging reaches AUTO only at the top of the range.

## The sweep on the real loop

Per row: three staging CPU spikes and three production memory leaks, each run
through the full loop, with a human approving every request.

| auto_decision_score | min_confidence | staging: AUTO / opened | production: AUTO / opened | unsafe actions |
|---|---|---|---|---|
| 0.75, 0.80, 0.85 | 0.50, 0.60, 0.70 | 2 / 2 | 0 / 2 | 0 |
| 0.90 | 0.50, 0.60, 0.70 | 0 / 2 | 0 / 2 | 0 |

min_confidence had no effect at any value tested, because the simulated RCA
confidence is always high. The sweep shows no unsafe action anywhere. It does not
say which threshold is optimal for real incidents: that needs labelled real
incidents.

## Detection boundaries (Agent 1, outside the policy)

- CPU: incidents open from 550 requests/s; 450 and 500 stay silent. The demo runs at 640.
- Memory leak: incidents open from 7 minutes of growth; 4–6 stay silent. The demo runs at 9.
- Healthy cluster, 20 minutes of normal traffic: **0 false alarms**.

These thresholds decide whether a fault is caught at all, so they matter more
than any policy weight for early detection.

## Stress test

| Run | Incidents | Resolved | Escalated | Wrong changes | Audit valid | Throughput | detect → decision p50 / p95 |
|---|---|---|---|---|---|---|---|
| 20, 30% duplicates, 5% Context failures | 20 | 19 | 1 | 0 | yes | 60 /min | 3.8 s / 5.6 s |
| 100, clean | 100 | 100 | 0 | 0 | yes | 60 /min | 25.7 s / 40.4 s |
| 100, 30% duplicates, 5% Context failures | 100 | 100 | 0 | 0 | yes | 52 /min | 27.6 s / 42.8 s |
| 100, 30% duplicates, 15% Context failures | 100 | 74 | 26 | 0 | yes | 44 /min | 26.2 s / 42.2 s |

What the numbers say:
- **No wrong change in any run** (one change per incident, or none), and every audit chain is valid.
- **RCA accuracy 1.0** in every run.
- **Escalations are safe.** At 15% Context failures, a write that fails five times in a row sends its message to the dead-letter topic and the incident escalates. Nothing executes. Raising `AIOPS_MAX_ATTEMPTS` or lengthening the backoff trades latency for fewer escalations.
- **Latency is queueing, not computation.** Agent tasks take well under a second; a burst of 100 incidents waits in line. Throughput stays near 50–60 per minute as load grows, so the ceiling is the single-process bus. On Kafka the limit becomes the topic partitions (3 per topic in the compose file). This was not measured on the Docker stack.

## What to tune, and what not to

- **Do not grid-search the decision weights against simulated outcomes.** The simulator defines what counts as correct, so the search would only recover the simulator's rules. The sweep above is flat across most of its grid, which is itself evidence that the defaults are not the problem.
- **Do tune the detection thresholds** if you need earlier detection, and check them against a false-alarm rate on real traffic. The healthy-cluster control is the minimum.
- **Do replace the constant historical success with data.** The Context store already records, for every action, whether it was verified. After enough real incidents, fit a calibrated model (logistic regression of "verified" on RCA confidence, evidence strength and agreement) and use its probability in place of the constant. Then choose the auto threshold against a cost you state explicitly (for example, one wrong production change costs far more than one unnecessary approval). That is the point where a grid search is meaningful.
- **Keep production human-gated.** Every test here agrees with the current rule; relaxing it should wait for real calibration data.
