# Agent 4 final engineering audit

Audit date: 2026-11-07
Branch: `Deployment-Agent`
Agent 4 commit audited: `3d60e5b754cbe0e7eeaae1c8ee9fab0f7f95705d` plus this working tree
Common base: `56c48f9667017db3e401227a04dafb0598df2f9b`
Latest fetched team main: `f228663f8d97d65349d45a3445264c6de76a9c2f`

## Verdict

**PASS for review and integration preparation; NOT PRODUCTION-READY.**

No live deployment was run. No cluster was contacted. The optional integration
harness remains intentionally skipped without explicit disposable-cluster
configuration. Real production activation remains blocked on the external
requirements listed below.

## Scope reviewed

The review covered contracts, canonical hashing, state machine, SQLite
transactions and workload exclusion, audit/outbox linkage, Kubernetes read and
dry-run adapters, pre-flight policy, patch construction, controlled mutation,
rollout observation, exact recovery, signed contracts and replay storage,
runtime composition, CLI, MCP, compatibility adapters, tests, documentation,
and the package-scoped GitHub Actions workflow. The current implementations on
team `main` for Agents 1, 2, 3, 5 and 6 were inspected without merging them.

## Findings and disposition

| Severity | Finding | Disposition |
|---|---|---|
| High | Agent 3's current schema adds `findings` arrays; the previous strict adapter rejected valid current assessments. | Resolved with strict redacted finding models and compatibility tests. |
| High | Journal integrity was checked by tooling but not revalidated at every final mutation/restart boundary. | Resolved; forward, rollback and restart paths now block before adapter access when integrity fails. Regression tests tamper delivery/audit linkage. |
| High | Agent 3 GitLeaks gate detected the synthetic idempotency fixture in `tests/integration/test_kubernetes_harness.py` under `generic-api-key`. | Current-tree fixture replaced with a constructed non-secret value. Historical committed detection remains covered by the narrow path/rule allowlist already on team `main`; no scanner policy was weakened here and no history was rewritten. |
| Medium | Agent 3 digest metadata did not prove source revision or freshness. | Resolved by requiring separate trusted provenance with exact revision, issue time and expiry. |
| Medium | Agent 5 `NO_CHANGE` could bypass exact workload resolution. | Resolved; it now requires one exact target and produces explicit `NO_OP`, still non-authorizing. |
| Medium | Current Agent 1/2/6 contracts had no explicit fail-closed Agent 4 adapters. | Resolved with strict evidence/proposal adapters and tests. Current Agent 1 evidence remains explicitly untrusted. |
| Medium | Outbox integration events omitted operation type, target hash, previous state and verification verdict. | Resolved and included in audit-chain verification. |
| Medium | CI tested PRs targeting the feature branch and used floating actions/dependencies; the first Windows-generated lock also lacked a Linux-safe marker for `pywin32`. | Resolved: PR target is `main`, actions are SHA-pinned, direct requirements are exact, CI installs a hash lock, `pywin32` is Windows-only, and a Linux wheel-resolution dry run passed. |
| Low | There was no single runnable lifecycle visualization. | Resolved with the offline demo covering success, rollback and restart reconciliation. |

No unresolved critical or high-severity code finding was identified in the
reviewed Agent 4 scope. This is not an independent penetration test.

## Verified safety properties

- Public CLI and MCP surfaces contain no live apply, execute, rollback, shell,
  arbitrary patch, trust-configuration, or caller-supplied kubeconfig route.
- Default runtime assembly is read-only and reports live execution unavailable.
- Controlled mutation requires explicit enablement, exact target/environment
  allowlists, a fresh hash-bound permit, trusted verifier identity, signed
  external permit ID, claims hash, and authenticated HITL provenance.
- Patch artifacts validate exact canonical bytes and permit only bounded paths.
- Signature verification is Ed25519 against injected public keys; no runtime
  signer or private-key fallback exists.
- Permit and verdict IDs are one-time durable records; conflicting replays fail.
- Idempotency and active workload exclusion are persistent database constraints.
- Audit and outbox writes occur transactionally with state/artifact changes.
- Uncertain Kubernetes outcomes reconcile exact state and are not blindly
  retried. Terminal and ambiguous restart paths fail closed.

## Cross-agent reality

- Agent 1 is implemented and emits monitoring recovery evidence, but not the
  signed execution-bound verdict Agent 4 requires.
- Agent 2 is implemented and emits typed action intents, but several current
  recommendations lack exact deploy/scale/rollback parameters.
- Agent 3 is implemented and remains the release-policy owner. Its assessment
  is not authorization and its digest still needs trusted CI provenance.
- Agent 5 is implemented and emits advisory resource recommendations. It does
  not authorize a mutation.
- Agent 6 is implemented and emits model lifecycle handoffs. Candidate files
  require trusted image packaging/provenance; rollback versions require an
  Agent 4 execution resolver.
- A shared production Orchestrator, HITL issuer/provenance service, Kafka
  deployment, context service, signed Agent 1 provider, and remote audit store
  are not implemented in this repository as usable Agent 4 dependencies.

## Production blockers and remaining risk

1. No reviewed live-mode composition root enables the mutation adapter.
2. No external authorization signer, key rotation/revocation, or authenticated
   HITL provenance provider is connected.
3. No signed Agent 1 provider/transport is connected.
4. No production outbox transport, consumer deduplication service, or remote
   immutable audit sink is connected.
5. SQLite backup, migration, retention, filesystem access control, and disaster
   recovery are operator responsibilities and are not exercised here.
6. Real cluster identity, least-privilege RBAC, admission behavior, network
   policy and rollout semantics have not been validated by this audit.
7. Canary, blue/green and weighted traffic routing are unsupported.
8. The feature branch is behind current team `main`; integration must account
   for the already-merged Agent 1/2/3/6 and repository changes.

## Validation record

- Pytest on Python 3.12.10 / pytest 8.4.2: **154 passed, 5 skipped, 0 failed**.
- Ruff 0.17.1: **All checks passed**.
- `pip check`: **No broken requirements found**.
- Hash-locked install: completed successfully with `--require-hashes`.
- Linux CPython 3.12 wheel-resolution dry run: completed successfully; no
  Windows-only package is selected.
- Current Agent 4 GitLeaks directory scan: **0 findings**.
- Workflow YAML parse, package imports, CLI help, MCP assembly and offline demo:
  passed.

Tests were run with a writable temporary base on Windows; the managed sandbox's
default pytest temporary directory can produce operating-system permission
errors unrelated to assertions. The five skipped tests are the explicitly
opt-in disposable Kubernetes harness, not simulated successes.
