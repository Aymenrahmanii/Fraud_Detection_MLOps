# Testing the platform: correctness, latency, performance, reliability

Five levels, from seconds to a full distributed run. All commands from the
repository root, with the environment from [README.md](README.md#run-it).
For every use case step by step, and a script for a live demo, see [DEMO.md](DEMO.md).

| Level | What it proves | Time |
|---|---|---|
| 1. Test suites (44 platform tests) | every component and the full loop behave as specified | ~1 min |
| 2. Offline demo | every v5 scenario end to end, with v5 §18 metrics | ~1 min |
| 3. Benchmark | latency percentiles, throughput, reliability under injected faults | ~30 s per run |
| 4. Interactive console | you play the operator; `/metrics` live | as long as you like |
| 5. Distributed stack + Grafana | the same loop over Kafka/gRPC/Postgres/Redis, real timings, chaos | ~5 min |

## 1. Test suites

```bash
python -m pytest -q -W ignore aiops_platform/tests        # platform: 44 tests
python -m pytest -q agent1_monitoring/tests               # each agent's own suite (unchanged)
python -m pytest -q agent2_diagnosis/tests
python -m pytest -q agent3_security_quality/tests API/tests
python -m pytest -q agent4_deployment_recovery/tests --ignore=agent4_deployment_recovery/tests/integration
python -m pytest -q agent5_resource_optimization/tests
python -m pytest -q agent6_mlops_lifecycle/tests/unit
```

The platform suite includes the reliability properties as tests: dedup of
redelivered messages, retry then success, DLQ metadata, poison messages,
concurrent writers vs the audit chain, forged service identity, wrong
approver, stale plan, and duplicate delivery after execution (`test_e2e.py`).

## 2. Offline demo

```bash
python -m aiops_platform.demo --auto
```

Runs the six v5 scenarios and prints, per environment: MTTA, MTTR, HITL rate,
autonomous resolution rate, action success / false action rate, policy
distribution, message reliability, audit-chain validity.

## 3. Benchmark: latency, throughput, reliability

```bash
python -m aiops_platform.experiments.benchmark -n 20                                      # clean run
python -m aiops_platform.experiments.benchmark -n 20 --duplicates 0.3 --context-failures 0.05
python -m aiops_platform.experiments.benchmark -n 20 --duplicates 0.3 --context-failures 0.15 --json report.json
```

Opens N concurrent incidents (N leaking services), approves every request
instantly, and reports:

* **latency** p50/p95/max: detection → decision, approval → verified recovery,
  and every agent task (`agent:objective`);
* **throughput**: incidents resolved per minute;
* **reliability**: resolved/escalated, RCA accuracy, *exactly one
  infrastructure change per incident*, audit chains valid, injected duplicates
  vs duplicates ignored, injected Context failures vs retries, dead letters,
  message reliability.

Fault knobs: `--duplicates p` redelivers each record with probability p (Kafka
at-least-once); `--context-failures q` makes each agent write to the Context
Service fail with probability q (dependency outage). `AIOPS_MAX_ATTEMPTS`
(default 5) is the retry budget before a message is dead-lettered. The exit
code is non-zero if a safety property is violated.

Reference results on a laptop (16 cores; in-process, verification windows
collapsed to zero, so these measure processing, not observation windows):

| Run | Resolved | detect→decision p95 | approval→recovery p95 | Throughput | Dead letters | Safety |
|---|---|---|---|---|---|---|
| 10 incidents, no faults | 10/10 | 2.7 s | 2.8 s | 71 /min | 0 | ✔ |
| 20, 30% duplicates, 5% Context failures | 20/20 | – | – | 65 /min | 0 | ✔ |
| 20, 30% duplicates, 15% Context failures | 13/20 (7 escalated) | – | – | 30 /min | 7 | ✔ |

At 15% failure on *every* Context write, some tasks exhaust their 5
attempts; those incidents escalate to a human (nothing executes), which is the
designed safe outcome. Agent 2's task dominates processing time
(evidence collection + RAG); Agent 4's execution includes signing checks,
dry-run, rollout observation and the signed Agent 1 verdict.

## 4. Interactive console

```bash
python -m aiops_platform.demo serve          # http://127.0.0.1:8090, token dev-sre-token
```

Inject faults with the buttons, approve/reject, watch the audit timeline.
Prometheus metrics of the whole in-process platform: <http://127.0.0.1:8090/metrics>.

## 5. Distributed stack, Grafana, chaos

```bash
docker compose -f compose.platform.yaml up --build -d
python aiops_platform/experiments/compose_smoke.py        # inject leak -> approve -> RECOVERED
```

| Where | What |
|---|---|
| <http://127.0.0.1:8090> | Dashboard (operators: `dev-sre-token`, `dev-release-token`, `dev-ml-token`) |
| <http://127.0.0.1:3001> | Grafana: dashboard **AIOps platform - latency, performance, reliability** |
| <http://127.0.0.1:9091> | Prometheus (Status → Targets shows every service) |
| <http://127.0.0.1:8080/metrics> | Orchestrator metrics; other services expose `:9100/metrics` inside the network |

Here timings are real: Agent 1 observes 60 s windows (Agent 4's signed
verdict, then the SLO verification), so approval → recovery is ~2 minutes.

Measured on the distributed stack (Prometheus averages, 2026-11-07):

| Step | Average |
|---|---|
| Agent 2 diagnosis (`identify_root_cause_and_recommend_action`) | 0.19 s |
| Agent 4 prepare + dry-run | 0.03 s |
| Agent 5 recommendation | 0.007 s |
| Agent 4 execute (incl. 60 s signed Agent 1 verdict window) | 45 s |
| Agent 1 SLO verification window | 45 s |
| Context Service call p95 (slowest: `UpdateIncident`) | 0.046 s |
| Incident MTTR, instant approval | 93 s |

Latency p95 panels need several events inside the rate window; with a few
demo incidents use the "avg" series on the same panels.

### Metrics (v5 §18 table)

| v5 metric | PromQL |
|---|---|
| MTTR | `histogram_quantile(0.95, sum by (le,kind) (rate(aiops_workflow_duration_seconds_bucket[15m])))` |
| Agent task latency / success | `aiops_agent_task_seconds{agent,objective,status}` |
| Kafka consumer lag | `max by (group) (aiops_kafka_consumer_lag)` |
| gRPC error / Context latency | `aiops_context_rpc_seconds{method,code}` |
| HITL waiting time / expiry | `aiops_hitl_wait_seconds`, `aiops_hitl_resolutions_total{state="EXPIRED"}` |
| Policy distribution | `aiops_policy_decisions_total{decision}` |
| False action rate | `aiops_actions_total{type,outcome}` (outcome ≠ RECOVERED) |
| Message reliability | `aiops_retries_total`, `aiops_dead_letters_total`, `aiops_duplicates_total` |

### Chaos experiments on the live stack

`C` = `docker compose -f compose.platform.yaml`. "Observed" = run on the live
stack on 2026-11-07; "tests" = covered by the automated suite / benchmark.

| Experiment | How | Expected | Status |
|---|---|---|---|
| Orchestrator restart while waiting for approval | `C restart orchestrator` while the approval is pending, then approve | resumes from its Postgres checkpoint, RESOLVED | ✔ observed |
| Context Service outage | `C stop context`, inject the leak, `C start context` after ~5 s | gRPC `UNAVAILABLE` → retries, 0 dead letters, RESOLVED | ✔ observed (2 retries) |
| Approve during the outage | approve while Context is down | HTTP 503 "nothing was approved or executed", approval still pending | ✔ observed (was a 500 before the fix) |
| Approval expiry | leave an approval pending past `ttl_seconds` (600 s) | EXPIRED, re-requested once, never executed | ✔ observed |
| Stale approved plan | approve after the target changed (e.g. cluster restarted) | Agent 4 refuses `RESOURCE_VERSION_MISMATCH`, escalated | ✔ observed |
| Agent 1 restart, new outage | `C restart agent-monitoring`, inject again | new incident ID, new workflow | ✔ observed + test (was silently dropped before the fix) |
| Duplicate delivery / duplicate execution request | benchmark `--duplicates 0.3`; `test_duplicate_delivery_has_no_second_side_effect` | duplicates ignored, exactly one change | ✔ tests |
| Wrong approver | approve with `dev-ml-token` | 403, nothing executes | ✔ tests |
| Agent crash mid-task | inject the leak, then `C restart agent-diagnosis` | uncommitted task redelivered after restart; incident resolves | not yet run live |

Stop and wipe: `docker compose -f compose.platform.yaml down -v`.
