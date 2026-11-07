# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: the jury, teachers and team members watching a **live demo / project
defense** of the six-agent system, usually on a laptop or desktop screen
(1280–1920 px). The presenter acts as the on-call operator: injects a fault,
watches the agents work, approves or rejects the proposed action, and shows
the verified outcome and the audit trail.

The surface must still read as a credible tool for a DevOps / SRE / MLOps
engineer (the role the presenter plays): triage incidents, judge a proposed
change, decide, and trace why the system did what it did.

## Product Purpose

The AIOps Control Room is the operator console of the "Intelligent Multi-Agent
System for Autonomous DevOps / MLOps Pipelines" (spec:
`Six_Agent_DevOps_MLOps_Technical_Architecture_v5.pdf`). It is where the human
in the loop sees incidents, releases and model checks moving through the
closed loop (detect → diagnose → decide → approve → execute → verify →
audit), and the only place a human approves or rejects an action. Success: a
viewer understands, from the screen alone, what broke, what the agents
concluded, why a human is needed, exactly what will change, and that the
result was verified and recorded.

## Positioning

Six specialist agents with one deterministic Orchestrator: every decision is
evidence- and policy-based (no LLM opinion averaging), every action is the
exact dry-run-validated patch a human approved (Ed25519 permit bound to plan
and patch hashes), every result is independently verified by Agent 1, and the
whole trail is a hash-linked audit chain.

## Operating Context

- Served by `aiops_platform/dashboard/app.py` (FastAPI) at
  http://127.0.0.1:8090, both in-process (`python -m aiops_platform.demo serve`)
  and in the distributed stack (`compose.platform.yaml`).
- Operators sign in with a bearer token mapped server-side to roles: `sre`
  (rollback/scale/memory), `release-manager` (deploy), `ml-lead` (model
  promotion), viewer (read-only). Dev tokens: `dev-sre-token`,
  `dev-release-token`, `dev-ml-token`, `dev-viewer-token`.
- Demo flow: inject a scenario (memory leak after deploy, CPU load spike,
  model drift check, reset) → incident appears → approval requested →
  approve/reject with a comment → recovery verified → audit trail.
- Companion tools during the demo: Grafana (:3001), Prometheus (:9091).

## Capabilities and Constraints

API the console consumes (no other data sources):

- `GET /api/me` → subject, roles
- `GET /api/incidents` → list (id, kind INCIDENT/RELEASE/MLOPS, status,
  priority, severity, environment, service, opened/closed, hitl state)
- `GET /api/incidents/{id}` → Context record (agent results, evidence refs,
  candidate actions, signals), workflow (stage, selected action, policy with
  risk factors and reasons, consensus with candidates and root cause,
  outcome, replans, approval, notes), hash-linked audit rows, audit-chain
  validity
- `GET /api/approvals?state=` → pending approvals with the request: action,
  parameters, environment, risk, decision score, reason, HITL triggers,
  evidence summary, plan (status, plan/patch hashes, target, exact RFC 6902
  patch), expiry
- `POST /api/approvals/{id}/resolve` {decision APPROVE|REJECT, comment ≥ 3
  chars} → 403 wrong role, 409 already resolved, 503 platform briefly
  unavailable (nothing approved)
- `POST /api/scenarios/{name}`, `GET /api/scenarios` (demo/sim only),
  `POST /api/releases`, `POST /api/model-checks/{model}`
- Stages: OPEN, DIAGNOSING, DECIDING, PLANNING, WAITING_FOR_APPROVAL,
  AUTHORIZED, EXECUTING, VERIFYING, REPLANNING, RESOLVED, ESCALATED,
  RELEASE_BLOCKED, ASSESSING_RELEASE, ASSESSING_MODEL, RETRAINING,
  PROMOTION_APPROVED, KEEP_CHAMPION.
- Static files only (`dashboard/web`: HTML/CSS/JS, no build step, no CDN
  dependency required); polling, no websockets.
- English UI.

## Brand Commitments

Name: **AIOps Control Room**. No logo or brand palette exists.

## Evidence on Hand

Real data comes only from the running platform (simulated cluster scenarios,
real agents). No customers, uptime figures or benchmarks may be invented in
the UI.

## Product Principles

1. The approval is the moment that matters: the exact change, its risk and the
   reasons a human is needed must be impossible to miss and impossible to
   misread.
2. Show the loop, not just records: a viewer should see where each workflow is
   in detect → diagnose → decide → approve → execute → verify.
3. Every claim on screen is traceable to an agent result, a policy reason or
   an audit record.
4. Safe by default: destructive or irreversible-looking controls demand
   intent (comment, role), and failures say plainly that nothing executed.
