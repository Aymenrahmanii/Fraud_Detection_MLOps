# Agent 4 architecture

## Purpose and boundary

Agent 4 converts an independently authorized, typed operation into a narrowly
bounded Kubernetes Deployment change. It does not decide global risk, issue
authorization, replace Agent 3 policy, replace Agent 5 optimization, diagnose
incidents, qualify models, or self-certify recovery. A mutation is successful
only after independent Agent 1 verification.

## Component map

```text
adapters/compatibility.py          Agent 3 and Agent 5 evidence adapters
adapters/agent{1,2,6}_compatibility.py
                                   current team-contract adapters
core/schemas.py                    immutable intent/approval/execution models
core/planner.py                    pure deterministic planning
core/preflight.py                  fail-closed safety checks
core/patch_builder.py              bounded RFC 6902 construction
core/coordinator.py                durable execution/recovery orchestration
adapters/kubernetes.py             exact read and dryRun=All boundary
adapters/kubernetes_mutation.py    private controlled mutation boundary
adapters/signed_contracts.py       Ed25519, HITL provenance and replay checks
persistence/sqlite_journal.py      transactions, locks, audit and outbox
adapters/outbox.py                 transport-neutral at-least-once dispatcher
mcp_server.py                      read-only MCP tools
runtime.py                         read-only operator composition root
demo/                              offline simulations only
```

## Trust and execution flow

```text
Agent 2/3/5/6 evidence
        |
        v
strict adapters -- proposals only, never authorization
        |
        v
external Orchestrator + independently authenticated HITL permit
        |
        v
authorization -> inspection -> pre-flight -> typed patch -> dryRun=All
        |
        v
fresh authorization -> audit-chain check -> exact mutation
        |
        v
rollout observation -> signed Agent 1 verdict -> terminal state
                              |
                              +-- failure: exact captured-state rollback
```

The Orchestrator, HITL issuer, Agent 1 signed-verdict transport, Kafka relay,
remote context service, and remote audit store are external requirements, not
services implemented in this package.

## Mutation invariants

- Default authorization denies, default mutation is disabled, and default
  recovery verification is `UNAVAILABLE`.
- Only namespaced Deployments and three forward mutation types are supported:
  digest image, replicas, and container resources.
- The exact target UID, `resourceVersion`, prior field values, approval scope,
  plan hash, rollback scope, and canonical patch bytes are bound before apply.
- The audit chain is checked immediately before forward/rollback mutation and
  before restart reconciliation. A mismatch returns an operator-required
  result and makes no infrastructure call.
- Kubernetes ambiguity is reconciled by reading exact state; an uncertain
  request is never blindly retried.
- Rollback is an inverse of captured prior state, not `rollout undo`.
- Canary, blue/green, weighted routing, arbitrary patch paths, Secrets,
  cluster-scoped resources, shell execution, and ambient kubeconfig fallback
  are unsupported.

## Persistence model

SQLite transactions atomically maintain execution state, immutable artifacts,
per-workload exclusion, idempotency, replay consumption, hash-linked audit
events, and outbox events. Outbox delivery uses leases and stable delivery IDs
with at-least-once semantics; consumers must deduplicate. The hash chain makes
tampering detectable but is not a substitute for independently administered
audit storage, backup, access controls, or retention.

## Supported readiness level

The package is suitable for offline review, contract integration, and a
disposable-cluster dry-run harness. The repository does not contain a reviewed
production live composition root, real authorization issuer, real Agent 1
provider, production event transport, or proven traffic-routing integration.
