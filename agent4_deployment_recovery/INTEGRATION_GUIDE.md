# Agent 4 integration guide

## Rule zero

Inputs from other agents are evidence or recommendations. They never authorize
Agent 4. An external Orchestrator must construct the final strict operation,
obtain independently authenticated human approval, and issue a signed permit
bound to the exact plan and patch.

## Agent 1 — monitoring and recovery verification

`adapters/agent1_compatibility.py` validates the current
`monitoring.recovery.verified` event. That event is deliberately returned as
untrusted evidence because it is unsigned and is not bound to Agent 4's
execution ID, target UID, plan hash, patch hash, or exact observation interval.

Production integration must implement `RecoveryVerdictProvider` and return the
signed contract in `core/integration_models.py`. Without it, verification is
`UNAVAILABLE` and a mutation cannot become `SUCCEEDED` or `ROLLED_BACK`.

## Agent 2 — diagnosis

`translate_agent2_diagnosis` accepts the current `DiagnosisCompleted` object,
selects one exact `action_id`, and resolves the logical service to one immutable
Kubernetes target. Only fully specified `DEPLOY`, `SCALE_REPLICAS`, and
`ROLLBACK` intents can become non-authorizing typed proposals. Current Agent 2
rollback/scale recommendations are often underspecified, so they correctly fail
closed until the Orchestrator supplies exact parameters through a trusted
resolver. Restart, canary, traffic shift, promotion, and blue/green intents are
unsupported.

## Agent 3 — security and release policy

`translate_agent3_assessment` accepts the current `ReleaseAssessment` including
redacted finding arrays. `BLOCK` always rejects. `ALLOW` and
`ALLOW_WITH_WARNING` remain evidence only. Digest deployment additionally
requires a separately trusted, fresh CI provenance result binding:

- the assessed source revision;
- the exact `repository@sha256:digest` image;
- issue and expiry timestamps; and
- a trusted verifier/reference.

The release assessment itself does not provide those guarantees.

## Agent 5 — resource recommendation

`translate_agent5_recommendation` requires exactly one target from an injected
resolver. Bounded scale and memory recommendations become typed proposals.
`NO_CHANGE` becomes an explicit `NO_OP`. Every result has
`constitutes_authorization=false` and still requires independent approval.

## Agent 6 — model lifecycle

The current model deployment request references a shared-volume candidate, not
a deployable image. `translate_agent6_deployment_request` therefore requires a
trusted resolver that attests the candidate SHA-256 to an immutable image and
model version, plus one exact serving workload. Rollback requests require a
trusted version-to-Agent-4-execution binding. The typed
`Agent6DeploymentOutcomeV1` is an output contract only; no Agent 6 callback
transport is implemented.

## Orchestrator, HITL and authorization

The signed authorization contract binds the audience, issuer, unique permit,
operation, environment, immutable target, plan and patch hashes, approval and
rollback scopes, and validity window. Signature validity alone is insufficient:
`ApprovalProvenanceVerifier` must authenticate a human-interactive approval
reference. Permit reservation and replay prevention are durable SQLite
transactions. The package contains verifiers only—no signer, private key,
approval UI, approval database, or test-key fallback.

## Events and Kafka

Each journal transition emits a versioned outbox event containing correlation
and trace IDs, execution/operation IDs, operation type, target identity hash,
previous/current state, verification verdict, and audit-chain linkage. A
transport-neutral dispatcher provides leases, retry and acknowledgement.
Kafka topics, credentials, brokers, schemas outside this package, and consumer
deployments are not implemented.

## Failure contract

Unknown schema fields, unsupported operations, ambiguous targets, stale
evidence, bad signatures, replays, approval mismatches, audit corruption,
Kubernetes conflicts/timeouts, and missing verification all fail closed.
Callers should surface the returned operator-required state and must not retry
mutations outside the coordinator.
