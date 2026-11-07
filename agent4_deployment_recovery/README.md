# Agent 4 — Intelligent Deployment & Recovery

This isolated package contains the Phase 0 contracts, Phase 1 safe core,
Phase 2 Kubernetes inspection/pre-flight boundary, Phase 3 controlled
execution engine, and Phase 4 authenticated cross-process contracts.
Phase 5 adds a private read-only composition root, a constrained MCP surface,
durable outbox delivery state, restart regression coverage, an opt-in
disposable-cluster harness, and a package-scoped CI quality gate.
Production mutation remains disabled by default. There is no
live CLI/MCP command, ambient Kubernetes context fallback, authorization
issuer, fabricated Agent 1 service, arbitrary patch input, shell, kubectl, or
manifest execution path.

## Implemented boundary

- Strict Pydantic contracts for authorized operations, immutable Kubernetes
  identity, approval and rollback scope, deterministic plans, execution
  records, recovery verification, typed patches, mutation permits, and rollout
  results.
- Five operation contracts: `DEPLOY_IMAGE_DIGEST`, `SCALE_REPLICAS`,
  `SET_CONTAINER_RESOURCES`, `ROLLBACK_EXECUTION`, and `NO_OP`.
- Exact approval-scope, plan, pre-flight, dry-run, patch-byte, rollout, and
  verification hashes.
- A transactional SQLite journal with unique idempotency keys, one persistent
  active execution per workload, optimistic transitions, immutable artifacts,
  append-only triggers, and a SHA-256-linked audit chain.
- Official-client Kubernetes inspection and `dryRun=All` for exact namespaced
  Deployments selected through an explicit kubeconfig/context or explicitly
  selected in-cluster configuration.
- Minimal RFC 6902 patches for image digest, replicas, and container resources,
  preserving UID, `resourceVersion`, container-name, and prior-value `test`
  operations.
- A separate controlled mutation adapter which accepts only a validated,
  persisted patch artifact and a fresh permit bound to its plan, approval
  scope, rollback scope, direction, and exact byte hash.
- Bounded rollout observation with success, timeout, stall, failed condition,
  stale generation/identity, API error, and cancellation outcomes.
- Exact inverse recovery for only the affected field. Recovery uses a fresh
  `resourceVersion`, proves the expected forward value, observes the rollback
  rollout, and requires independent Agent 1 `VERIFIED` evidence before
  `ROLLED_BACK`.
- Strict Ed25519 authorization and Agent 1 envelopes, configured only with
  injected public-key issuer registries. Runtime code contains no signer,
  private key, self-issued receipt, test-key fallback, or ambient trust source.
- Durable one-time permit/verdict consumption and a transactional SQLite
  outbox. Each typed event references the corresponding hash-chain sequence;
  no Kafka or gRPC publisher is claimed or implemented.
- Atomic authorization reservations: permit replay consumption, binding to the
  operation/plan, external-authorization artifact, audit event, and outbox
  event either commit together or all roll back. A restart before mutation can
  reuse only that persisted exact reservation.
- Durable outbox delivery metadata with stable delivery IDs, transactional
  claims, expiring leases, bounded retries, and explicit acknowledgements.
  Delivery is at-least-once; every consumer must deduplicate by `delivery_id`.
- A private runtime loader that reads a fixed operator environment variable
  pointing to an absolute configuration file. Callers cannot inject keys,
  verifiers, Kubernetes clients, allowlists, permits, or configuration JSON.
- A read-only MCP server. Its default assembly has no Kubernetes adapter,
  journal reader, read-access verifier, or live execution capability.
- Evidence-only Agent 3 and Agent 5 translators. Agent 3 requires a separate
  trusted-CI provenance verifier for digest deployment; Agent 5 requires an
  exact workload resolver and still produces only a proposed typed operation.
- Read-only standalone rollback preparation which validates a recoverable
  source execution and forward artifact, proves the current forward state,
  and returns a guarded inverse preview requiring separate authorization.

`ROLLBACK_EXECUTION` has read-only preparation only. There is no standalone
operator-triggered rollback command or live apply route. Automatic Phase 3
recovery remains limited to the pre-authorized rollback attached to a forward
execution and its captured immutable artifact.

## Execution state flow

The controlled path is:

```text
RECEIVED -> AUTHORIZED -> PREFLIGHT_PASSED -> PLANNED -> DRY_RUN_PASSED
         -> EXECUTING -> OBSERVING -> AWAITING_AGENT1 -> SUCCEEDED
```

`SUCCEEDED` is impossible for a mutation without trusted Agent 1 `VERIFIED`
evidence. A timeout/connectivity ambiguity enters `OUTCOME_UNCERTAIN`; Agent 4
re-reads UID, generation, `resourceVersion`, and exact affected fields and
never blindly retries. Divergence keeps the workload lock and requires operator
reconciliation.

Failure recovery is:

```text
ROLLBACK_PENDING -> ROLLING_BACK -> ROLLBACK_AWAITING_AGENT1 -> ROLLED_BACK
                                      \-> ROLLBACK_FAILED
```

An unsafe inverse, expired rollback scope, conflict, failed rollout, or missing
trusted recovery evidence produces `ROLLBACK_FAILED` and escalation. An
in-flight uncertain rollback remains locked for operator reconciliation rather
than being retried.

## Safety invariants

1. Authorization is externally verified on receipt and again immediately
   before each mutation. Agent 3 assessments and Agent 5 recommendations are
   evidence only.
2. The default authorization verifier denies all live actions, the default
   mutation adapter performs no API call, and the default Agent 1 verifier
   returns `UNAVAILABLE`.
3. The exact canonical RFC 6902 bytes accepted by server-side dry-run are
   persisted before `EXECUTING`. The live adapter cannot accept a caller patch.
4. The immediate pre-mutation read must still match UID, `resourceVersion`,
   generation, and captured prior values. The patch retains server-enforced
   RFC 6902 tests to close the remaining read/apply race.
5. Idempotency and per-workload exclusion are SQLite constraints, not merely
   process locks. Restart recovery inspects persisted state and does not repeat
   an in-flight mutation.
6. Kubernetes rollout convergence is operational evidence only; it is never
   substituted for Agent 1 recovery verification.
7. Canary, blue/green, weighted routing, cluster-scoped resources, Secrets,
   arbitrary paths, and unqualified rollout undo are unsupported.
8. A valid issuer signature is not HITL proof. The authorization verifier also
   requires an independently authenticated approval-provenance reference.
9. The production mutation adapter requires a signed external permit, trusted
   verifier ID, explicit environment allowlist, and exact target allowlist.
   Injected test fakes never satisfy this adapter accidentally.

## Operation-specific behavior

| Operation | Phase 3 behavior |
|---|---|
| `DEPLOY_IMAGE_DIGEST` | Requires immutable current/new digests, readiness probe, native rolling update, and exact prior image. |
| `SCALE_REPLICAS` | Requires observed replicas to equal `from_replicas`; image digest is irrelevant. |
| `SET_CONTAINER_RESOURCES` | Validates resulting request/limit bounds and captures exact presence/value semantics; rollback removes a field that was originally absent. |
| `NO_OP` | Authorized and journaled without Kubernetes mutation or Agent 1 verification. |
| `ROLLBACK_EXECUTION` | Read-only exact-source lookup and inverse preview; separate authorization is mandatory and no live entry point exists. |

The repository's current `K8s/fraud-api-deployment.yaml` remains unsafe for a
live image rollout because it uses a mutable image and lacks a readiness probe.
Local manifests do not prove RBAC or traffic-routing capability.

## CLI

The CLI remains read-only/offline except for explicit inspection and
non-persisting server dry-run. It has no `apply`, `execute`, `deploy`,
`rollback`, shell, or kubectl command.

```powershell
python -m agent4_deployment_recovery.cli validate-operation operation.json
python -m agent4_deployment_recovery.cli plan operation.json
python -m agent4_deployment_recovery.cli validate-plan plan.json
python -m agent4_deployment_recovery.cli inspect --snapshot deployment_snapshot.json
python -m agent4_deployment_recovery.cli preflight operation.json deployment_snapshot.json policy.json
python -m agent4_deployment_recovery.cli patch-preview operation.json deployment_snapshot.json policy.json

# Explicit cluster contact, still read-only/non-persisting:
python -m agent4_deployment_recovery.cli inspect --operation operation.json --connection connection.json --contact-cluster
python -m agent4_deployment_recovery.cli server-dry-run operation.json policy.json connection.json --contact-cluster
```

## Read-only MCP tools

The default `python -m agent4_deployment_recovery.mcp_server` surface exposes
`get_capabilities`, `validate_typed_operation`, `preview_deployment_plan`,
`check_agent3_evidence`, and `check_agent5_evidence`.

An operator-assembled service may additionally expose
`preflight_patch_preview`, but its inspector/context is fixed at assembly time;
the MCP caller cannot supply a kubeconfig or context. `get_execution_status`
and `get_audit_events` are registered only when an authenticated read-access
verifier and journal are both injected by the private host. Status returns a
target hash rather than target details, and audit output omits event payloads.
No MCP assembly registers deploy, execute, apply, rollback, shell, trust
configuration, arbitrary patch, or live server-dry-run tools.

## Local setup and quality gate

Use an isolated Python 3.12 environment; never install these packages into the
global interpreter:

```powershell
py -3.12 -m venv .venv-agent4
.\.venv-agent4\Scripts\python.exe -m pip install -r agent4_deployment_recovery\requirements.txt
.\.venv-agent4\Scripts\python.exe -m pip install ruff
.\.venv-agent4\Scripts\python.exe -m pytest agent4_deployment_recovery\tests -v
.\.venv-agent4\Scripts\ruff.exe check agent4_deployment_recovery
.\.venv-agent4\Scripts\python.exe -m pip check
```

`.github/workflows/agent4-quality.yml` runs Python 3.12 tests, Ruff, and
dependency consistency checks only for Agent 4 path changes. It has read-only
repository permission and contains no deployment, image publishing, cluster
connection, production secret, or integration-test opt-in.

## Simulation

Simulation uses only injected fakes and temporary SQLite files. It does not
claim real-cluster authorization, admission, rollout, or Agent 1 verification.

```powershell
python -m pytest agent4_deployment_recovery/tests/test_phase3_coordinator.py -v
python -m pytest agent4_deployment_recovery/tests/test_phase3_recovery.py -v
python -m pytest agent4_deployment_recovery/tests/test_phase3_rollout.py -v
python -m pytest agent4_deployment_recovery/tests/test_phase3_mutation_adapter.py -v
```

The simulations cover success, denial and re-authorization failure, stale
state, timeouts before/after apply, restart reconciliation, rollout failure,
exact rollback, rollback conflict, all Agent 1 verdict classes, journal failure,
idempotency, and persistent workload concurrency. No fake is imported by the
runtime package.

## External contracts and team dependencies

The `1.0` signed authorization envelope binds issuer, audience, unique permit,
operation/correlation IDs, environment, exact Kubernetes target, typed
parameters, plan and exact patch hashes, authorization/rollback scopes, and a
bounded validity window. Ed25519 verification uses an injected key registry.
The referenced approval receipt is separately checked through an injected
provenance verifier; Agent 4 does not implement or impersonate the future
Orchestrator/HITL provider.

The signed Agent 1 contract binds a one-time verdict to a persisted request,
execution/correlation IDs, workload UID, plan/patch hashes, expected image or
version fields, request time, observation interval, and expiry. Only
`VERIFIED`, `FAILED`, and `INCONCLUSIVE` may be signed. With no trusted Agent 1
connection, the existing default remains `UNAVAILABLE` and cannot produce a
successful mutation state.

Agent 3's current `ReleaseAssessment` does not contain authenticated CI
provenance, issue/expiry timestamps, or authorization. `BLOCK` always rejects;
`ALLOW_WITH_WARNING` remains evidence only. Digest deployment additionally
needs a trusted CI attestation resolving the assessment digest to the exact
repository digest. Agent 5's current `ResourceRecommendation` is likewise
advisory: only bounded `SCALE_REPLICAS` and `INCREASE_MEMORY` values are
translated after unambiguous workload resolution; `NO_CHANGE` produces no
operation. Neither adapter executes source JSON.

## Explicit prerequisites for any real live-mode composition

- An externally operated authorization issuer, public-key distribution and
  rotation, revocation policy, and independently operated HITL receipt source.
- An authenticated Agent 1 transport/provider and trusted public-key lifecycle.
- Explicit Kubernetes cluster identity, target allowlist, least-privilege
  ServiceAccount/RBAC, bounded network policy, and verified API capabilities.
- An operator-owned persistent journal location with backup, retention, and
  access controls; SQLite hash linking is detectable tamper evidence, not an
  independently administered audit service.
- A separately reviewed private live composition root that may enable
  `OfficialControlledMutationAdapter`; Phase 5's composition root remains
  read-only even when it validates a complete `FUTURE_LIVE` configuration.
- A production transport/relay and consumer deduplication store. Phase 5
  provides dispatcher semantics and an in-memory test transport only; it does
  not fabricate Kafka, gRPC, or a remote audit store.

## Disposable Kubernetes integration tests

The optional harness lives under `integration/kubernetes`. It is disabled
unless an operator sets `AGENT4_K8S_INTEGRATION` to the exact deliberate opt-in
value and supplies an absolute kubeconfig, context, cluster identity, and exact
target identity. Its manifest creates only a disposable namespace,
ServiceAccount, namespaced Role, and RoleBinding. The Role is restricted by
`resourceNames` and permits only Deployment `get` and `patch`; patch calls in
the harness use `dryRun=All`.

See `integration/kubernetes/README.md` for prerequisites and cleanup. These
tests cover exact read, admission dry-run/no persistence, stale version
rejection, RBAC denial, and bounded rollout observation. They are intentionally
skipped in normal local and CI runs and must never target production.

## Team integration requirements and remaining gaps

- The Orchestrator must issue the strict typed operation and signed permit,
  preserve correlation/idempotency fields, and bind it to the exact plan and
  patch. A separate trusted HITL adapter must authenticate the referenced
  human receipt. Missing either integration denies mutation.
- Agent 3 must provide its current release-assessment JSON; CI must separately
  attest the exact immutable digest. `ALLOW` and `ALLOW_WITH_WARNING` are never
  authorization, while `BLOCK` always stops translation.
- Agent 5 must provide its current recommendation JSON. The Orchestrator must
  resolve its logical service to exactly one immutable Kubernetes target and
  independently authorize any proposal. `NO_CHANGE` never becomes a mutation.
- Agent 1 must provide an authenticated signed `VERIFIED`, `FAILED`, or
  `INCONCLUSIVE` response bound to the persisted request and target. Missing
  trusted transport/key configuration yields `UNAVAILABLE`.
- CI supplies quality evidence and an external artifact attestation; Agent 4's
  workflow neither authorizes nor deploys.
- Operations must provide a production transport/relay, consumer deduplication,
  journal backup/access control, monitoring, retention, and an operator
  reconciliation process for uncertain or rollback-failed states. Kafka,
  gRPC, a remote audit store, and that operational UI do not exist here.
- Add a separately authorized, dry-run-gated execution workflow for prepared
  standalone rollbacks before considering any live rollback endpoint.
- Run the opt-in harness against a designated disposable cluster and document
  the results before claiming real-cluster/RBAC compatibility. No such run is
  claimed by this package.
- Add a real traffic-routing provider before canary or blue/green delivery;
  replica ratios must never emulate weighted routing.

Production readiness additionally requires key rotation/revocation, secure
operator configuration distribution, journal migration/backup testing,
transport observability, SLOs, incident runbooks, recovery drills, and an
independent security review. Phase 5 is integration-ready for review, not a
production deployment authorization.

## Quality commands

```powershell
python -m pytest agent4_deployment_recovery/tests -v
python -m ruff check agent4_deployment_recovery
python -m pip check
```
