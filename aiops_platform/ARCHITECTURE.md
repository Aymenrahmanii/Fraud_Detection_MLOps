# Shared platform (`aiops_platform`)

The six agents are merged on `main`. This package is the **shared platform** from
the v5 architecture (`Six_Agent_DevOps_MLOps_Technical_Architecture_v5.pdf`,
sections 2-9 and 17-18): everything that connects the agents into one closed
loop. It is not a seventh agent and contains no specialist analysis.

## Rules this package keeps

1. **Agents are not modified.** Each agent keeps its own contracts and entry
   point. A thin *worker* per agent (`aiops_platform/workers/`) translates
   between the platform contract and the agent's own models.
2. **Orchestrator reads, agents append.** Only the Orchestrator calls
   `GetIncidentContext` / `GetLatestAgentResults`. Workers can only call
   `AppendAgentResult` and `AppendEvidence`. The Context Service rejects anything else.
3. **One workflow owner.** The Orchestrator (LangGraph) owns routing, consensus,
   policy, HITL pause/resume and the continuation sent to Agent 4. The Dashboard
   never calls Kubernetes or an agent; its decision goes to the Orchestrator,
   which resolves it through the HITL Service (v5 §7).
4. **Deterministic first.** Routing, consensus and the risk gate are rules
   (v5 §17). No LLM in the platform.
5. **Every transport has an in-memory twin.** Unit and integration tests run
   without Docker; `compose.platform.yaml` runs the same code on Redpanda (Kafka API),
   PostgreSQL and Redis.

## Components

| Module | v5 section | Responsibility |
|---|---|---|
| `contracts/` | §4.2, §5, §10 | Canonical v1 envelope, payloads, task/result, topics. Same shapes as `agent1_monitoring/shared_contracts` (identical to Agent 2's copy); `AgentResult.payload` added as in Agent 6. |
| `bus/` | §3, §18 | `EventBus` protocol; `InMemoryBus` and `KafkaBus` (aiokafka). Consumer groups, `incident_id` partition key, idempotent consumers, retry topics with exponential backoff + jitter, DLQ with §18.1 metadata. |
| `context/` | §6 | Context Service: PostgreSQL/SQLite store (incidents, agent_results, actions, approvals, model_events, evidence_metadata, decisions, audit), object store for large evidence, gRPC server with per-identity ACL. |
| `hitl/` | §7, §8 | HITL Service (gRPC): approvals with expiry, operator-role authorization, Ed25519 permits in **Agent 4's own `SignedAuthorizationEnvelopeV1` format** bound to the exact plan and patch hashes, and an `ApprovalProvenanceVerifier` for Agent 4. |
| `policy/` | §17 | DecisionScore / RiskScore and the AUTO / APPROVAL_REQUIRED / BLOCK gate, configured in `policy/policy.yaml`. |
| `orchestrator/` | §9 | LangGraph supervisor graph with checkpoints (Postgres or memory): ingest → route → dispatch → aggregate → consensus → policy → HITL interrupt → execute → verify → close/replan. Incident, release and MLOps flows. |
| `workers/` | §11-16 | One worker per agent: consume task topic → call the agent → append result/evidence → publish the typed result event. |
| `dashboard/` | §7 | Operator UI: incidents, audit timeline, pending approvals with Approve/Reject. |
| `observability/` | §18 | Platform metrics (task latency, consumer lag, HITL wait, policy distribution, false-action rate) and correlation-id tracing. |
| `experiments/` | §18 eval | Fault-injection scenarios and MTTD / MTTA / MTTR / HITL-rate measurement. |

## Topics

The v5 topic table (§3.2) is used as-is. The table has no topic for the
Orchestrator to *dispatch a task* to a specialist, so one is added per agent:

| Topic | Producer | Consumer |
|---|---|---|
| `agent.task.<agent-id>` (e.g. `agent.task.diagnosis-agent`) | Orchestrator | that agent's worker |
| `<topic>.retry` / `<topic>.dlq` | any consumer | retry relay / operators |

## Agent 4 execution path (why there is a "prepare" step)

Agent 4 only mutates with a signed permit that binds `plan_hash`, `patch_hash`
and the target's exact `resourceVersion`. Those hashes come from a live
snapshot, so approval has to happen **after** the exact patch is known:

```
Orchestrator --task(prepare)--> Agent 4 worker: plan + preflight + dry-run, no mutation
Orchestrator <--plan/patch hashes--
Orchestrator --CreateApproval--> HITL --> Dashboard (human sees exact patch)
Dashboard --APPROVE--> Orchestrator --ResolveApproval--> HITL: signs permit over those hashes
Orchestrator --workflow.continuation (permit)--> Agent 4 worker: coordinator.execute()
Agent 4 --verify request--> Agent 1 (signed verdict) --> Agent 4 terminal state
```

AUTO-approved actions still go through the HITL Service, which then signs with
`AUTOMATED_POLICY` provenance. Agent 4 rejects that for live mutation by design,
so AUTO is only allowed where Agent 4 allows it (non-production, bounded scale).

AUTO-approved actions get a permit signed with `AUTOMATED_POLICY` provenance,
carried on `deployment.action.requested`. In production the Agent 4 worker
composes the coordinator with Agent 4's own `SignedAuthorizationPermitVerifier`
and the HITL provenance verifier, which demand `HUMAN_INTERACTIVE`, so AUTO
can never mutate production; elsewhere the HITL token verifier is enough.

### Agent 1's verdict for Agent 4

Agent 4's coordinator blocks until it holds a *signed* Agent 1 verdict bound to
its execution (plan hash, patch hash, target UID). That is a short typed
request, so it is a gRPC call (`MonitoringService.VerifyExecution`, served by
the Agent 1 worker), not a Kafka round trip. Afterwards the Orchestrator still
dispatches its own `verify_recovery` task (v5 step 10) before closing.

## Synchronous calls and identity (v5 4.1, 18)

`proto/aiops_platform/rpc/gen/platform.proto` defines `ContextService`,
`HITLService` and `MonitoringService` with the v5 RPC names. Contract values
travel as their v1 JSON inside the messages, so pydantic/JSON Schema remain
the single source of validation. The gRPC clients implement exactly the
in-process interfaces, so nothing above the transport changes.

Each process holds one service credential (`role:name` + HMAC token issued at
deploy time); the server derives the `Principal` from call metadata, never from
the request body, and `ContextService` enforces the access matrix. Production
swaps the HMAC tokens for mTLS workload identity.

## Deployment

* **In-process** (`runtime.py`): tests, `python -m aiops_platform.demo`.
* **Distributed** (`compose.platform.yaml`, `services.py`): Redpanda,
  PostgreSQL (Context + LangGraph checkpoints), Redis (idempotency), Context,
  HITL, Orchestrator (+ HTTP API for the Dashboard), Dashboard, one container
  per agent, and the simulated cluster as a service. Signing keys stay in their
  own service; public keys are shared via a read-only `/trust` volume.

## Deployment target

No Kubernetes cluster is assumed. `sim/cluster.py` provides simulated
Deployments behind Agent 4's own inspector/mutation interfaces, with faults
bound to releases (rolling back v2.7 removes its leak) and telemetry derived
from release, load and replicas. A real cluster replaces it through Agent 4's
existing `adapters/kubernetes*.py` and Agent 2's live `KubernetesTool`.
