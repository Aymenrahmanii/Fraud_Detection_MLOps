# Shared platform: Orchestrator, backbone, Context, HITL, Dashboard

This package turns the six merged agents into the closed loop described in
`Six_Agent_DevOps_MLOps_Technical_Architecture_v5.pdf`. It is the shared
platform (v5 sections 2â€“9, 17â€“18), not a seventh agent: it routes work, holds
state, gates risk and asks humans; the agents do all the specialist work.
Design notes are in [ARCHITECTURE.md](ARCHITECTURE.md).

```
Agent 1 â”€â”€ANOMALY_DETECTEDâ”€â”€â–¶ Kafka â”€â”€â–¶ Orchestrator (LangGraph, one thread per incident)
                                          â”‚  task_context            â–² typed result events
                                          â”œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ Agent 2 RCA â”€â”€â”€â”¤
                                          â”œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ Agent 5 alternative
                                          â”‚  consensus â†’ Policy & Risk Gate (AUTO / APPROVAL / BLOCK)
                                          â”œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ Agent 4 prepare: exact plan + dry-run (no change)
                                          â”‚  HITL Service â—€â”€â”€â–¶ Dashboard (human sees the exact patch)
                                          â”œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ Agent 4 execute (signed permit) â”€â”€gRPCâ”€â”€â–¶ Agent 1 signed verdict
                                          â”œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¶ Agent 1 verify over the SLO window
                                          â””â”€â–¶ close / replan / escalate      every step â†’ Context Service (audit chain)
```

## Run it

All commands from the repository root. The agents' own tests are unchanged.

| What | Command |
|---|---|
| Platform tests (44: contracts, bus, Context, policy, HITL, gRPC, Dashboard, end-to-end loop) | `python -m pytest -q -W ignore aiops_platform/tests` |
| Scripted demo of every v5 scenario + evaluation metrics (no Docker) | `python -m aiops_platform.demo --auto` |
| Platform + Dashboard in one process, with fault-injection buttons | `python -m aiops_platform.demo serve` â†’ <http://127.0.0.1:8090> |
| Distributed stack (Redpanda, Postgres, Redis, gRPC, 13 containers) | `docker compose -f compose.platform.yaml up --build -d` â†’ <http://127.0.0.1:8090> |
| Distributed smoke test (inject leak â†’ approve â†’ verified recovery) | `python aiops_platform/experiments/compose_smoke.py --start --stop` |

Dashboard tokens (development only, `dashboard/operators.example.yaml`):
`dev-sre-token` (approves rollback/scale), `dev-release-token` (deploys),
`dev-ml-token` (model promotion), `dev-viewer-token` (read-only).

Installing everything in one environment (as CI does):

```bash
grep -v -E '^(cachetools|cryptography|pywin32)==' agent6_mlops_lifecycle/constraints-tested.txt > /tmp/constraints.txt
uv pip install -c /tmp/constraints.txt --override docker/overrides.txt \
  -r requirements.txt -r agent1_monitoring/requirements.txt -r agent2_diagnosis/requirements.txt \
  -r agent3_security_quality/requirements.txt -r docker/agent4-requirements.txt \
  -r agent5_resource_optimization/requirements.txt -r aiops_platform/requirements.txt
```

## What the demo shows (v5 MVP boundary, section 18.1)

1. **Memory leak introduced by a deployment**: Agent 1 detects growth on
   `payment-service v2.7`; Agent 2 finds the rollout in Kubernetes history and
   ranks `MEMORY_LEAK_POST_DEPLOY`; Agent 5 proposes scale-up, scored as
   temporary containment; the gate requires a human (production rollback);
   Agent 4 prepares and dry-runs the exact image-digest patch; the SRE approves
   in the Dashboard; the HITL Service signs a permit bound to that patch;
   Agent 4 executes, gets a signed Agent 1 verdict, and Agent 1 independently
   verifies recovery; the incident closes with a verified hash-chained audit trail.
2. **CPU saturation (staging)**: bounded scale-up is AUTO, no human needed.
3. **Failed test before deployment**: Agent 3's `BLOCK` stops the release.
4. **Release after the fix**: `ALLOW` â†’ approval â†’ deploy â†’ verification.
5. **Data drift**: Agent 6 detects drift and F1 drop, retrains with its sklearn
   pipeline, compares candidate vs champion, registers the candidate in MLflow,
   and the ML lead approves the promotion review.
6. **Duplicate delivery / rejection / wrong approver / stale plan**: covered by
   the tests (no second side effect, replan without execution, 403, Agent 4 refusal).

## What is real and what is simulated

| Real code paths | Simulated |
|---|---|
| All six agents' own logic (Agent 1 `MonitoringAgent` + recovery `Verifier`, Agent 2 `DiagnosisAgent` with its `KubernetesTool` and RAG, Agent 3 gate, Agent 4 `ExecutionCoordinator`/preflight/patch builder/rollout observer/signed-contract verifiers, Agent 5 optimizer, Agent 6 assessor + sklearn pipeline + MLflow registry) | The Kubernetes cluster and its telemetry (`sim/cluster.py`): Deployments behind Agent 4's own inspection/mutation interfaces, release-bound faults, `kubectl get -o json` answers for Agent 2 |
| Orchestrator, policy gate, HITL Service, Ed25519 permits, Context Service, audit chain, Dashboard | In simulation, Agent 1's *detection* is simple SLO/growth rules over the simulated signals (its full Prometheus/Loki detector stack needs the live lab, `compose.agent1.yaml`) |
| Kafka (Redpanda), gRPC, PostgreSQL, Redis, LangGraph Postgres checkpoints in compose | Agent 3 normally receives the CI assessment (its workflow already produces it); local scanning is opt-in (`AGENT3_LOCAL_SCAN=true`) |

## Contract changes (platform copy only; agents keep theirs)

`aiops_platform/contracts` is the canonical v1 contract: Agent 1's copy
(identical to Agent 2's) plus:

* `AgentResult.payload` (as in Agent 6's copy): the typed domain output.
* Topics `agent.task.<agent-id>`, `agent.task.result`, `deployment.plan.prepared`;
  retry topics are per consumer group (`<topic>.retry.<group>`).
* `ActionType.INCREASE_MEMORY` (Agent 5's memory recommendation).
* An `AUTO` `ActionRequest` may carry the HITL policy permit: Agent 4 mutates
  nothing without a signed permit, and it still refuses `AUTOMATED_POLICY`
  provenance in production.
* `IncidentContext` gains the v5 Â§6.3 fields (all optional).

## Findings for the agent owners

* **Agent 6** (fixed, separate commit): `SklearnTrainingPipeline` failed at
  `mlflow.sklearn.log_model` with the pinned mlflow 3.17 / sklearn 1.6, because
  skops rejected `ColumnTransformer`'s `_RemainderColsList`. The unit tests stub
  training, so only the end-to-end retrain flow hit it.
* **Agent 1 / Agent 6 dependency conflict**: `drain3` pins `cachetools==4.2.1`,
  mlflow 3 needs `>=5`. One environment needs the override in
  `docker/overrides.txt`; Agent 1's full suite passes with it.
* **Agent 4**: its exact pins (`cryptography==46.0.7`, `pydantic==2.14.1`)
  conflict with the shared lock; its 154 tests pass on the shared versions.
  Its `ROLLBACK_EXECUTION` only undoes its own runs, so incident rollbacks are
  resolved to `DEPLOY_IMAGE_DIGEST` of the previous release.
* **Agent 2**: rollback recommendations carry no target version; the platform's
  resolver uses the deployment history (as Agent 4's integration guide asks).
* **Model promotion**: Agent 4 cannot deploy a model (no image for a candidate),
  so promotion ends at a human-approved registry decision recorded in the
  Context Service; building/rolling out the serving image is the next step.

## Layout

```
aiops_platform/
  contracts/      canonical v1 contracts and topic table
  bus/            EventBus, ReliableConsumer (dedup, retry, DLQ), memory + Kafka transports
  context/        Context Service store, access rules, object storage, clients
  policy/         Policy & Risk Gate + policy.yaml
  hitl/           HITL Service, Ed25519 keys, Agent 4 provenance verifier
  orchestrator/   LangGraph graph, consensus, service, HTTP API
  workers/        one worker per agent + Agent 4 composition (executor)
  rpc/            gRPC Context / HITL / Monitoring services (proto in /proto)
  dashboard/      operator console (FastAPI + static UI)
  sim/            simulated cluster, scenarios, cluster-as-a-service
  experiments/    evaluation metrics, compose smoke test
  runtime.py      in-process composition; services.py: distributed entry points
```
