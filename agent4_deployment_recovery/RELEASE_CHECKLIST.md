# Agent 4 release checklist

## Source and scope

- [ ] Branch is `Deployment-Agent`.
- [ ] Latest `origin/main` was fetched; no merge/rebase was performed implicitly.
- [ ] Only `agent4_deployment_recovery/**` and
  `.github/workflows/agent4-quality.yml` contain intended changes.
- [ ] Agent 1/2/3/5/6, API, shared Kubernetes manifests, and original CI files
  have no working-tree modifications.
- [ ] No file is staged before review approval.

## Security

- [ ] GitLeaks current-tree scan has zero findings; historical finding handling
  uses only the existing narrow main-branch rule/path allowlist.
- [ ] No private key, token, kubeconfig, SQLite database, environment, cache,
  report, absolute machine path, or generated test directory is included.
- [ ] Default authorization denies; default mutation is disabled; default Agent
  1 verification is unavailable.
- [ ] Audit-integrity tamper tests block forward mutation and restart.
- [ ] No live CLI/MCP mutation tool or unrestricted patch path exists.

## Reproducible quality commands

```powershell
.\.venv-agent4\Scripts\python.exe -m pip install --require-hashes -r agent4_deployment_recovery\requirements-ci.txt
.\.venv-agent4\Scripts\python.exe -m pytest agent4_deployment_recovery\tests -v
.\.venv-agent4\Scripts\python.exe -m ruff check agent4_deployment_recovery
.\.venv-agent4\Scripts\python.exe -m pip check
.\.venv-agent4\Scripts\python.exe -m agent4_deployment_recovery.demo
```

- [x] Pytest: **154 passed, 5 skipped, 0 failed** on Python 3.12.10 with
  pytest 8.4.2; skips are the explicit disposable-cluster harness.
- [x] Ruff: **All checks passed** (`ruff 0.17.1`).
- [x] `pip check`: **No broken requirements found**.
- [x] Hash lock: local `--require-hashes` install and Linux CPython 3.12
  manylinux wheel-resolution dry run both passed.
- [ ] Offline demo reports success, exact rollback, restart reconciliation, and
  valid audit chains with `cluster_contacted=false`.
- [ ] Disposable Kubernetes tests remain skipped unless a designated non-
  production cluster is explicitly configured. No real-cluster success is
  claimed when skipped.

## Integration approval

- [ ] Agent 3 release decision is reviewed separately and remains authoritative
  for security policy.
- [ ] Orchestrator/HITL, Agent 1 signed verification, production outbox and
  audit-store gaps are accepted as production blockers.
- [ ] Reviewers accept that this release is integration-ready, not authorization
  to deploy to production.
- [ ] Sync with team `main` is performed only after preserving this working tree
  and reviewing the integration diff.
