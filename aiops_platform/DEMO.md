# Demo runbook: testing every use case yourself, and the live demo

Every result below was observed on this code with the commands shown. Commands
are for Windows PowerShell from the repository root; on Linux/macOS replace
`.\.venv\Scripts\python.exe` with `python`.

Two ways to run the platform:

| | In-process demo | Distributed stack (Docker) |
|---|---|---|
| Start | `.\.venv\Scripts\python.exe -m aiops_platform.demo serve --port 8092` | `docker compose -f compose.platform.yaml up -d --build` |
| Open | <http://127.0.0.1:8092> | <http://127.0.0.1:8090> |
| What runs | all six agents, Orchestrator, policy, HITL, Agent 4, in one process | the same code as separate containers over Kafka, gRPC, Postgres, Redis |
| Timings | observation windows shortened: a loop closes in about 1 s | real: Agent 1 watches 60 s windows, MTTR about 93 s |
| Scenarios | memory leak, CPU spike, model drift, release (failing test), release (fixed) | the same five, plus *Reset cluster* |
| Use it for | the use cases, rehearsing, the live demo | the use cases with real timings, Grafana, chaos experiments |

Every use case below works on both. On Docker, rebuild after pulling new code
(`docker compose -f compose.platform.yaml up -d --build`) and expect real
durations: an incident appears within about a minute of the fault, and a change
takes about 90 s from approval to verified recovery (45 s rollout check by
Agent 1 for Agent 4, then a 45 s SLO window).

**More workflows.** Each release or model-check click is a new workflow. Faults
open at most one incident per service (payment-service and checkout-api) while
it is open; once it closes, the same button opens a new one. *Reset cluster*
returns the simulated cluster to its healthy state.

Operator tokens (the role decides what you may approve):

| Token | Who | May approve |
|---|---|---|
| `dev-sre-token` | alice@sre | rollback, scaling; may run scenarios |
| `dev-release-token` | rita@release | deployments |
| `dev-ml-token` | max@ml | model promotion |
| `dev-viewer-token` | viewer | nothing (read-only) |

Restarting the demo server resets it to a clean state. Port 8090 belongs to the
Docker dashboard when the stack is up, so run the demo server on another port.

Without the buttons, the same workflows start through the API, e.g. a release
(the release manager's token):

```powershell
$body = '{"service_id":"payment-service","namespace":"prod","current_version":"v2.6","version":"v2.8","commit":"c0ffee2","assessment":{"assessment_id":"A-1","commit":"c0ffee2","artifact":{"image":"registry.local/payment-service:v2.8"},"tests":{"total":124,"passed":124,"failed":0,"skipped":0,"coverage_pct":87.3},"quality":{"gate":"PASSED"},"security":{},"decision":"ALLOW","blocking_reasons":[]}}'
Invoke-RestMethod -Method Post http://127.0.0.1:8090/api/releases -Headers @{Authorization="Bearer dev-release-token"} -ContentType application/json -Body $body
Invoke-RestMethod -Method Post http://127.0.0.1:8090/api/model-checks/machine-failure -Headers @{Authorization="Bearer dev-ml-token"}
```

---

## 0. What each scenario is for

Each scenario exercises one part of the platform. The reasoning below follows the
agents and the policy gate step by step, with the rules from `policy/policy.yaml`:
an action runs by itself only when its risk is low and its decision score is at
least 0.85; a production change needs a human unless it is pre-authorized (only
replica scaling in staging, test and local is); a human can only approve with the
role the action requires.

| Scenario | What it tests | Human approval | Expected end state |
|---|---|---|---|
| Memory leak after deploy | a bad release, and whether the platform rolls back instead of masking it | yes: SRE | RESOLVED, recovery verified, v2.6 running |
| CPU load spike | a real capacity problem: should the service scale out? | yes in production (staging scales alone) | RESOLVED after approval; ESCALATED if rejected |
| Release v2.8 (failing test) | a broken build must never reach production | no | RELEASE_BLOCKED, nothing deployed |
| Release v2.8 (fixed) | a good build still needs the owner of deployments | yes: release manager | RESOLVED, v2.8 running |
| Model drift check | a model whose data changed is retrained, but the champion is replaced only by a person | yes: ML lead | PROMOTION_APPROVED; no infrastructure changed |

### Memory leak after deploy

1. Agent 1 sees memory at 81% and climbing after the v2.7 deploy, while error rate and latency are still fine. The growth rule fires and the workflow opens.
2. The Orchestrator asks Agents 2 and 5. Agent 2 ranks **MEMORY_LEAK_POST_DEPLOY** first at 70%, citing 10 evidence records, including the deploy itself. Agent 5 finds no resource change justified: raising limits would hide the leak.
3. Two candidates remain: a rollback (score 0.70) and a scale-up (0.35). The rollback wins.
4. Policy: the rollback is medium risk (0.45) and its decision score (0.67) is below 0.85, so it is not automatic. Production is never pre-authorized, so a human must approve.
5. Agent 4 turns the rollback into an exact patch (image v2.7 → v2.6, with preconditions on the object's UID and resourceVersion) and dry-runs it against the live object.
6. An SRE approves. The approval becomes a signed permit for that exact plan; Agent 4 applies only that patch, and Agent 1 verifies over a one-minute window: error rate, latency and memory growth must all pass.

**Expected:** RESOLVED, outcome RECOVERED, payment-service on v2.6, audit chain verified. On Docker, about 90 s from approval to verified recovery.
**If nobody answers:** the approval is requested twice (600 s each by default), then the workflow escalates with reason *HITL expired*. Nothing is executed.

### CPU load spike

1. Agent 1 sees checkout-api saturating its CPU under a traffic spike.
2. Agent 2 concludes **resource pressure / CPU starvation** and proposes scaling (score 0.98). Agent 5 proposes scaling from 3 to 5 replicas (0.95).
3. Policy: scaling is low risk, but production is not pre-authorized, so a human must approve.
4. Agent 4 refuses Agent 2's version: *scale intent has no exact replica count*. Agent 5's version (3 → 5) is a complete plan and goes to the human.
5. A human then decides:
   - **Approve:** the service scales to 5 replicas and Agent 1 verifies recovery.
   - **Reject:** the Orchestrator never proposes the same change again. The only other option is refused by Agent 4, so the workflow escalates with *Rejected by human*. Nothing is executed.

**Staging contrast:** `--auto` runs the same spike in staging. Scaling is pre-authorized there, so the workflow goes straight to AUTO and resolves without a human. Use it to show bounded autonomy.

### Release v2.8, failing test

1. Agent 3 runs the tests, the quality gate and the security scanners: 123 of 124 tests pass.
2. The gate decides BLOCK with the reason *1 failing test*. The release workflow stops there: no candidate action, no approval, nothing deployed.

**Expected:** RELEASE_BLOCKED within about 2 s, and zero infrastructure changes. No human is involved, which is the point: the gate needs no approval to say no.

### Release v2.8, fixed

1. Agent 3 allows the release: 124 of 124 tests pass, coverage 87.3%.
2. The Orchestrator proposes a deployment. It has the widest blast radius of the actions (0.6) and targets production, so approval is required.
3. An SRE tries to approve and is refused (403): deployments belong to the release manager.
4. The release manager approves. Agent 4 prepares and dry-runs the deployment, applies it, and Agent 1 verifies v2.8 against the SLO.

**Expected:** RESOLVED, v2.8 running, about 90 s on Docker from approval to verified recovery.

### Model drift check

1. Agent 6 measures the drift on recent data: drift is detected (score 1.31), and the decision is RETRAIN_AND_EVALUATE.
2. It trains a challenger and compares it with the champion: F1 0.89 against 0.71. The result is REQUEST_PROMOTION_REVIEW, never an automatic promotion.
3. The ML lead reviews the comparison and approves. The workflow ends in PROMOTION_APPROVED.

**Expected:** PROMOTION_APPROVED and no infrastructure changed: promotion is a governance decision, recorded in the audit trail. Be ready to say the F1 values come from a small demo dataset.

## 1. The use cases

Start a fresh server for this section, with a short approval lifetime so that
expiry (use case 6) takes about a minute instead of 20:

```powershell
.\.venv\Scripts\python.exe -m aiops_platform.demo serve --port 8092 --approval-ttl 30
```

Sign in with `dev-sre-token`. Every workflow opens a sheet on the right; click
any of the seven steps at its top (01 Detect … 07 Close) to see that phase in
full: who acted, the agent's output, timings, evidence and every audit record.

### UC1. Memory leak after a deploy → rollback with human approval

1. Click **Memory leak after deploy**.
2. Within a second: `payment-service` is **Waiting for approval**; the red block
   shows *Roll back payment-service* and the exact image change v2.7 → v2.6.
3. Click **01 Detect**: Agent 1's detection (memory 81.3%, the rule that fired).
   **02 Diagnose**: Agent 2's root cause *Memory leak post deploy* (70%), its
   hypotheses and 10 evidence records; Agent 5 found no resource change justified.
   **03 Decide**: consensus, ranked candidates, the policy gate (*approval
   required*: production side effect, risk medium 0.45), Agent 4's dry-run plan.
4. Write a comment, click **Approve and execute**.
5. About a second later: **Recovered and verified**. **05 Execute** shows the
   cluster before/after (v2.7 → v2.6); **06 Verify** shows Agent 1's SLO checks
   (error rate, latency, memory growth: pass).

Proves: the full closed loop, human approval bound to the exact patch, verified recovery.

### UC2. Role separation: the wrong person cannot approve

On a pending rollback (repeat UC1 up to step 2):

| Signed in as | Result |
|---|---|
| `dev-ml-token` / `dev-release-token` | Approve/Reject disabled; the band says *Needs sre; you are ml-lead* |
| `dev-viewer-token` | read-only; cannot run scenarios either (HTTP 403) |
| `dev-sre-token` | approves |

The server enforces it, not only the buttons: an API call from the wrong role
returns `403 APPROVER_NOT_AUTHORIZED: max@ml needs one of ['admin', 'sre'] to
resolve ROLLBACK`, and approving twice returns `409 APPROVAL_NOT_PENDING`.

### UC3. Rejection: nothing executes, and the same change is not asked again

1. Click **CPU load spike**: `checkout-api` waits for approval to *Scale* 3 → 5.
2. Write a comment, click **Reject**.
3. A few seconds later: **Escalated to a human, nothing further was executed**,
   reason *Rejected by human*. **07 Close** lists why: the re-plan proposed the
   same scale-up again (*no new option: a human already rejected SCALE_REPLICAS*),
   and **03 Decide** shows Agent 4 refusing the other option, a scale-up without a
   replica count. Nothing was left, so the workflow escalated.

Proves: a human "no" is final for that change; Agent 4 refuses vague plans.

### UC4. Release blocked by Agent 3's gate

1. Click **Release v2.8 (failing test)**.
2. Immediately: **Release blocked by Agent 3's gate**; nothing was deployed.
   **02 Diagnose** shows Agent 3's assessment: decision *BLOCK*, reason *1 failing test*.

### UC5. Fixed release: deploy with the release manager's approval

1. Click **Release v2.8 (fixed)**: waits for approval to *Deploy*.
2. As `dev-sre-token` the buttons are disabled (*Needs release-manager*).
3. Sign out, sign in with `dev-release-token`, approve.
4. About a second later: deployed and verified by Agent 1.

### UC6. Model drift: retrain, champion/challenger, ML lead review

1. Click **Model drift check** (sign in as `dev-sre-token` to run it).
2. About 25-30 s: Agent 6 detects drift, retrains a challenger, compares it with
   the champion (F1 0.89 vs 0.71) and asks for a promotion review.
3. Approve as `dev-ml-token` → **Promotion approved**. No infrastructure changes:
   promotion is a governance decision.

### UC7. Nobody answers: the approval expires, nothing executes

1. Click **Memory leak after deploy** and do nothing.
2. After 30 s the approval expires and is re-requested once; after another 30 s
   the workflow is **Escalated after** about 1 minute, reason *HITL expired*.
   **04 Approve** shows both requests and both expiries.

On the Docker stack, the lifetime is `AIOPS_APPROVAL_TTL_SECONDS` (default 600);
rebuild the orchestrator once so it has this setting:
`$env:AIOPS_APPROVAL_TTL_SECONDS=60; docker compose -f compose.platform.yaml up -d --build orchestrator`.

### UC8. Tamper evidence: editing the audit trail is detected

Run the demo server with a known state directory, close one workflow (UC1), then
change who approved it, directly in the database:

```powershell
.\.venv\Scripts\python.exe -m aiops_platform.demo serve --port 8092 --workdir .\demo-state
# after UC1, in another terminal:
.\.venv\Scripts\python.exe -m aiops_platform.experiments.tamper_audit .\demo-state
```

It prints which record it changed (*approved by mallory@attacker, was
alice@sre*). Reload that workflow: the audit trail header turns red, *Hash chain
broken: records were altered* (before: *Hash chain verified*). Stop the server and
delete `demo-state` afterwards.

### UC9. Every scenario in one scripted run, with the v5 §18 metrics

```powershell
.\.venv\Scripts\python.exe -m aiops_platform.demo --auto
```

Prints the five scenarios (leak/rollback, CPU in staging resolved autonomously,
blocked release, fixed release, model promotion) and, per environment: MTTA,
MTTR, HITL rate, autonomous resolution rate, action success / false action rate,
policy distribution, message reliability, audit-chain validity.

---

## 2. Reliability, performance, metrics

**Benchmark** (in-process; latency percentiles, throughput, fault injection):

```powershell
.\.venv\Scripts\python.exe -m aiops_platform.experiments.benchmark -n 20
.\.venv\Scripts\python.exe -m aiops_platform.experiments.benchmark -n 20 --duplicates 0.3 --context-failures 0.05
```

What to point at: *one change per incident=True* and *audit chains valid=True*
even with 30% duplicate messages and failing Context writes; *injected Context
failures → retries*; any incident that exhausts its retries escalates with
nothing executed. Timings here exclude observation windows.

**Grafana and Prometheus** (Docker stack):

| Where | What |
|---|---|
| <http://127.0.0.1:3001> | Grafana: dashboard *AIOps platform - latency, performance, reliability* |
| <http://127.0.0.1:9091> | Prometheus; Status → Targets lists every service |
| <http://127.0.0.1:8080/metrics> | Orchestrator metrics; the demo server exposes `/metrics` on its own port |

Useful queries: `aiops_agent_task_seconds_sum / aiops_agent_task_seconds_count`
(average per agent task), `aiops_policy_decisions_total` (AUTO / APPROVAL_REQUIRED
/ BLOCK), `aiops_retries_total`, `aiops_dead_letters_total`. With only a few
incidents, use the *avg* series; p95 needs several events in the window.

**Chaos on the live stack** (`C` = `docker compose -f compose.platform.yaml`):

| Experiment | How | Expected |
|---|---|---|
| Orchestrator crash during an approval | `C restart orchestrator` while waiting, then approve | resumes from its Postgres checkpoint, resolves |
| Context Service outage | `C stop context`, inject the leak, `C start context` within ~5 s | retried, nothing lost, resolves |
| Approve during the outage | approve while Context is down | HTTP 503 *nothing was approved or executed*; approval still pending |
| Watch the agents talk | `C logs -f orchestrator agent-diagnosis agent-deployment-recovery` | each task and result as it happens |
| See the messages | `C exec redpanda rpk topic list`, then `C exec redpanda rpk topic consume diagnosis.completed -n 1` | the topics of v5 §3.2 and a real event |

---

## 3. A 10-minute live demo

Before: start the demo server (`--port 8092`), open the dashboard, sign in as
`dev-sre-token`, keep a second browser profile signed in as `dev-release-token`.
Have the Docker stack and Grafana open in other tabs if you show them.

| Min | Do | Say |
|---|---|---|
| 0-1 | Point at the stage strip and the empty register | "Six agents, one closed loop: detect, diagnose, decide, approve, execute, verify, close. The orchestrator is deterministic; no LLM routes decisions." |
| 1-3 | UC1 up to the red block | "Agent 1 detected it, Agent 2 found the cause, the policy gate says production needs a human. This is the exact patch Agent 4 dry-ran; my approval signs a permit bound to its hashes." |
| 3-4 | Click 02 Diagnose, then 03 Decide | "Every phase is inspectable: the agent's typed output, the evidence it cites, the policy reasons." |
| 4-5 | Approve; open 05 Execute and 06 Verify | "Agent 4 applied only that patch; Agent 1 verified recovery independently." |
| 5-6 | Audit trail: hash chain verified | "Every step is hash-chained; editing one record breaks the chain." (UC8 if time allows) |
| 6-7 | UC3: CPU spike, Reject | "A human no is final: nothing executes, the same change is never asked again." |
| 7-8 | UC4 then UC5 in the release-manager window | "Agent 3 blocks a release with a failing test; the fixed build needs the release manager, not the SRE." |
| 8-9 | Grafana | "Agent latency, policy distribution, retries and dead letters, live." |
| 9-10 | Benchmark output (prepared beforehand) | "20 concurrent incidents with duplicated messages and failing writes: exactly one change each, every audit chain valid." |

Run UC6 (model drift, ~30 s; longer on Docker) early in the background if you
want to show it, and keep the `--auto` output from section UC9 ready as a backup.
On Docker, start UC1 a minute before you need it: detection waits for Agent 1's
window, and approval to recovery takes about 90 s.

---

## 4. Questions a jury may ask

- **Why are the demo timings so short?** The in-process demo shortens Agent 1's
  observation windows. On the Docker stack they are real: about 93 s from
  detection to verified recovery with instant approval.
- **What if the Context Service is down?** Each message is retried (5 attempts,
  about 11 s); then it goes to a dead-letter topic and the incident escalates to a
  human. Nothing executes without its records. A longer outage budget is one setting
  (`AIOPS_MAX_ATTEMPTS`).
- **What stops a duplicate message from acting twice?** Consumer dedup by message
  ID, content-addressed evidence, and Agent 4's idempotency key; the benchmark
  checks *exactly one change per incident* under 30% duplicates.
- **Can an approval be reused or forged?** The permit is an Ed25519 signature over
  the exact plan and patch hashes, checked by Agent 4 before acting; a stale plan
  (cluster changed since the dry-run) is refused.
- **Why is the model F1 so round (0.89 vs 0.71)?** The demo dataset is small;
  promotion still requires the ML lead's review.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `error while attempting to bind on address ... 8090` | the Docker dashboard has 8090: use `--port 8092` |
| The page looks unstyled or old | `Ctrl+F5` once; on Docker, `docker compose -f compose.platform.yaml up -d --build dashboard` |
| No scenario buttons | sign in with `dev-sre-token` (viewers cannot run scenarios) |
| An approval disappeared | it expired (`--approval-ttl`); run the scenario again |
| A workflow list looks empty | a stage filter is active (a black cell in the top strip): click it again or *Clear filters* |
| Start over | stop the server (`Ctrl+C`) and start it again; on Docker, *Reset cluster* or `docker compose -f compose.platform.yaml down -v` |
