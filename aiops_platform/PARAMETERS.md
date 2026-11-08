# Every parameter: where it is, where it comes from, and its status

Status key:
- **Spec**: the value or formula is written in the v5 architecture document.
- **Config**: set in a config file with a comment explaining it.
- **Hand-picked**: our choice, justified by reasoning, not calibrated against data.
- **Placeholder**: stands in until real outcome data exists.

No value here is claimed to be optimal. The v5 document itself says the decision
weights are "calibrated experimentally, not fixed by design", and Agent 1's
config says its thresholds are "starting values, to be calibrated on the test bench".

## 1. Decision score and risk (`aiops_platform/policy/policy.yaml`, `policy/gate.py`)

| Value | What it does | Status | Justification |
|---|---|---|---|
| 0.40 RCA confidence | weight in the decision score | Spec (v5 §17, example) | The leading hypothesis matters most. The spec lists it first. |
| 0.25 evidence strength | weight | Spec (example) | Strong evidence should count, but less than the diagnosis itself. |
| 0.20 historical success | weight | Spec (example) | Past success of the same action class. Counts only once history exists. |
| 0.15 agent agreement | weight | Spec (example) | Corroboration between agents, the smallest of the four. |
| 0.5 historical success default | value used while there is no history | Placeholder | Neutral prior. It adds a fixed 0.10 to every score, so no action can learn from past outcomes yet. |
| 0.30 environment criticality | weight in risk | Hand-picked | The largest risk factor: the same action is riskier in production. |
| 0.20 blast radius, 0.20 irreversibility, 0.15 data impact, 0.15 uncertainty | weights in risk | Hand-picked | The spec names these factors but gives no weights. They sum to 1.0. |
| production 1.0, staging 0.4, test 0.1, local 0.0 | environment criticality | Hand-picked | Ordinal: what a mistake costs in each environment. |
| per-action blast radius (scale 0.2, rollback 0.5, deploy 0.6, blue-green 0.7) | action profile | Hand-picked | Ordinal: how much a wrong action affects. Rollback is wider than scaling, deploy wider still. |
| 0.35 / 0.60 / 0.80 risk level cutoffs | LOW, MEDIUM, HIGH | Hand-picked | Divides the score into four bands. With the 0.30 environment weight, production is always at least MEDIUM. |
| 0.85 auto threshold | score needed for AUTO (with LOW risk and no trigger) | Hand-picked | Deliberately conservative. Our sweep shows the most sensitive value. With the placeholder history, AUTO needs RCA confidence of about 0.94. |
| 0.60 minimum RCA confidence | below this, a human decides | Config; the rule is in the spec ("decision confidence below the configured threshold") | The spec requires human review on low confidence; the 0.60 value is ours. |
| 0.50 max disagreement | agreement below 0.5 forces a human | Hand-picked | Any incompatible pair of proposals (agreement 0.3) therefore goes to a human. |

**Agreement table** (`orchestrator/consensus.py`): 1.0 when both agents propose compatible actions; 0.8 when only one speaks; 0.75 when a containment action sits next to a root-cause fix; 0.3 when proposals are incompatible; and capped at 0.45 when Agent 2 asks for more evidence. Hand-picked ordinal values. The reason for the ordering: contradiction is the worst case, containment beside a fix is compatible but weaker.

**Candidate scores** (`orchestrator/consensus.py`): Agent 2's score is its RCA probability, halved for each lower-ranked recommendation. Agent 5's is its confidence, halved when the root cause is one more capacity cannot fix (memory leak, crash loop, pool exhaustion, dependency down). Hand-picked heuristics, justified by the v5 scenario "scale-up is a temporary alternative to rollback".

## 2. Detection (`aiops_platform/workers/agent1.py`, `SIM_RULES`)

The live loop uses `SimAnomalyDetector`, not Agent 1's full statistical and forecasting pipeline. Its rules:

| Value | Signal | Status | Justification |
|---|---|---|---|
| 2% error rate | `error_rate` | Config (Agent 1 `max_error_rate: 0.02`) | Same limit Agent 1 uses to verify recovery. The detector and the verifier agree on what "bad" means. |
| 500 ms p95 latency | `latency_p95_ms` | Config (Agent 1 `max_latency_p95_ms: 500`) | Same reason. |
| 5 MB/min memory growth, over 75% used | `memory_slope_mb_min`, `memory_pct` | Config for 5 MB/min (Agent 1 `max_memory_slope_mb_min: 5`); 75% hand-picked | A steady climb is caught before it reaches the absolute limit. |
| 85% memory, 85% CPU | absolute limits | Hand-picked | Leave headroom before saturation. Not calibrated. |
| any pod restart | OOM | Hand-picked | One restart after memory pressure is already a failure. |
| score 0.55 + 0.12 × symptoms | detection confidence | Hand-picked | Grows with the number of symptoms. |

**Measured boundaries** (`experiments/POLICY_TUNING.md`): CPU opens an incident from 550 requests/s; the leak from 7 minutes of growth; no false alarms over 20 minutes of normal traffic.

**Not used on the live path:** Agent 1's multi-window burn-rate rules (36 / 14.4 / 6, the Google SRE workbook values) and its statistical and forecasting detectors exist in `agent1_monitoring/` and are configured there, but the platform does not wire them in yet. Connecting them is the next step if the defense needs them.

## 3. Operations (`orchestrator/graph.py`, `hitl/service.py`, `bus/reliability.py`, `policy.yaml`)

| Value | What it does | Status | Justification |
|---|---|---|---|
| 600 s approval window | time for a human to decide | Hand-picked (`AIOPS_APPROVAL_TTL_SECONDS`) | Ten minutes for an on-call response. Expired approvals are asked once more, then escalate. |
| 5 retry attempts | before a message goes to the dead-letter topic | Hand-picked (`AIOPS_MAX_ATTEMPTS`) | Covers brief outages (about 11 s here). Longer outages escalate, which is the safe outcome. Measured: 15% failures escalate about a quarter of incidents. |
| 2–8 replicas, at most 4 per step | bounds on automatic scaling | Hand-picked guardrail | 2 keeps a minimum for availability; 8 caps cost and blast radius; 4 prevents large jumps. |
| SCALE_REPLICAS pre-authorized in staging, test, local | automatic actions allowed | Hand-picked | Staging is where a wrong scale is cheap. Production is never pre-authorized. |
| BLUE_GREEN_SWITCH forbidden in production | hard block | Hand-picked | Its blast radius is the highest in the table (0.7): it moves all traffic at once. |
| Agent 3 coverage floor 70% | release gate | Hand-picked | A common industry floor. Our test runs report 86–87%. |
| Agent 3 BLOCK on any failing test or secret | release gate | Spec ("deterministic scan/test results remain authoritative") | Failing tests or leaked secrets cannot be overridden by a model's opinion. |

## 4. Where a language model is used

The v5 document says: "Implement deterministic MVP behavior before adding LLM/advanced ML." It also says LLM output is "untrusted suggestions until schema validation", and that the Orchestrator "should not simply average LLM opinions".

**On the default demo path (what you run), no language model makes a decision.** Verified in the code:

| Agent | Default (platform path) | Optional, off by default | Model if enabled | How to enable |
|---|---|---|---|---|
| Orchestrator | deterministic LangGraph workflow | none | none | none: routing is never an LLM |
| Agent 1 (monitoring) | rule-based detector (above) | log labelling with Gemini; forecasting with Chronos | Gemini `gemini-2.5-flash`; Chronos `amazon/chronos-bolt-tiny` (a time-series model, not an LLM) | `PIDS_GEMINI_API_KEY`; Chronos `enabled: true` in its config |
| Agent 2 (diagnosis) | deterministic rules and a local JSON knowledge base matched by tags | Claude as a supplemental analyzer; a Qdrant vector index | Claude, default `claude-opus-5-5`; Qdrant uses a deterministic hashing embedder, not a model | `LLM_*` environment variables; the platform worker does not enable it |
| Agent 3 (security and quality) | deterministic gate; release rationale is `null` | rationale sentence via Groq | `openai/gpt-oss-20b` | `GROQ_API_KEY` |
| Agent 4 (deployment) | deterministic plan, dry-run and signed permit | none | none | none |
| Agent 5 (resources) | deterministic rules | none | none | none |
| Agent 6 (MLOps) | deterministic promotion policy | advisor and LLM judge for text evaluation | `openai/gpt-oss-20b` via Groq | `advisor_enabled: true` (default false), provider keys |

**Justification for the choice of deterministic first:**
- Every decision is reproducible and auditable from its recorded inputs.
- The benchmark and sweep results apply to the actual decision path, and the hash-chained audit can explain each one without a model in the loop.
- A language model would add cost, latency and an extra failure mode to every incident, for no measured gain in the simulated scenarios.

**When an LLM would be justified:** for Agent 2 on messy, unseen incidents, where the rules find no known signature. Measure it first: run the same labelled scenarios with and without the analyzer, and adopt it only if it wins without producing unsafe proposals.

## 5. What is still not justified

- **The auto threshold (0.85) and the decision weights** are not calibrated. Calibration needs labelled real outcomes: the Context store already records whether each action was verified, so this data accumulates in use.
- **The historical success placeholder (0.5)** means the platform cannot yet learn from its own history.
- **The detection thresholds** (85% memory, 85% CPU, the 75% growth gate) are hand-picked. Test them on real traffic against a false-alarm budget.
- **The agreement table** is ordinal and hand-set. A sensitivity sweep over it would show whether its ordering ever changes a decision.
- **Agent 5 and Agent 6 thresholds** were not reviewed in this pass.

**Recommended experiment for the defense: an ablation.** Remove one gate at a time (for example, the production pre-authorization rule, or the release gate) and count the unsafe actions the sweep would then allow. That shows why each gate exists, which is a stronger argument than any weight value.
