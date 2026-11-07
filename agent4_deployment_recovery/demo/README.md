# Agent 4 offline demonstration

Run from the repository root:

```powershell
.\.venv-agent4\Scripts\python.exe -m agent4_deployment_recovery.demo
```

The demo uses the production planner, pre-flight engine, patch builder,
coordinator, state machine, and SQLite journal. Every external trust or
infrastructure boundary is an explicit in-memory fake. It demonstrates a
successful deployment, a failed rollout followed by exact-state rollback, and
restart reconciliation without contacting Kubernetes or accepting real
authorization.
