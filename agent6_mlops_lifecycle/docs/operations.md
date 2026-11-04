# Configuration, operations and integration contracts

## Private configuration

Keep runtime settings in `agent6_mlops_lifecycle/.env`, independent of other agents. Process environment takes precedence. Public defaults are typed in `monitoring/settings.py` and `lifecycle/settings.py`.

| Setting | Default / purpose |
| --- | --- |
| `GROQ_API_KEY` | Optional advisor key; never a dashboard credential |
| `AGENT6_MONITOR_TOKEN` | Generated operator credential for dashboard and reviews |
| `AGENT6_MONITOR_OUTCOME_TOKEN` | Distinct generated credential for trusted business outcomes |
| `AGENT6_LIFECYCLE_INTEGRATION_TOKEN` | Distinct generated credential for the deployment agent |
| `AGENT6_MONITOR_INTERVAL_SECONDS` | 5 seconds |
| `AGENT6_MONITOR_WINDOW_SECONDS` | 3,600 seconds for current serving observations |
| `AGENT6_MONITOR_QUALITY_WINDOW_SECONDS` | 604,800 seconds to accommodate delayed confirmations |
| `AGENT6_MONITOR_MIN_SAMPLES` | 30 confirmed labels, with class support and coverage gates |
| `AGENT6_MONITOR_MIN_OUTCOME_COVERAGE` | 0.5 |
| `AGENT6_MONITOR_MIN_QUALITY_SCORE` | 0.8; illustrative until calibrated |
| `AGENT6_MONITOR_BASELINE_MODE` | `auto`; initial reference is observational, not a quality certification |
| `AGENT6_MONITOR_BASELINE_WARMUP_SECONDS` | 60 seconds |
| `AGENT6_MONITOR_MIN_DISTRIBUTION_SAMPLES` | 100; PSI threshold 0.2 |
| `AGENT6_MONITOR_MANUAL_REVIEW_ENABLED` | `false`; business confirmations normally arrive automatically |
| `AGENT6_MONITOR_ADVISOR_INTERVAL_SECONDS` | 900; unchanged evidence is cached |
| `AGENT6_LIFECYCLE_ENABLED` | `true`; automatic candidate preparation |
| `AGENT6_LIFECYCLE_MIN_VERIFIED_LABELS` | 100, at least 20 of each class |
| `AGENT6_LIFECYCLE_MIN_GUARD_LABELS` | 50, at least 5 of each class |
| `AGENT6_LIFECYCLE_COOLDOWN_HOURS` | 24 per model |
| `AGENT6_LIFECYCLE_MAX_JOBS_PER_DAY` | 2 per scoped lifecycle store |
| `AGENT6_LIFECYCLE_TIMEOUT_SECONDS` | 300 per candidate worker |
| `AGENT6_LIFECYCLE_MIN_F1`, `MIN_RECALL` | 0.85 and 0.90, respectively; both require the full lifecycle prefix |
| `AGENT6_LIFECYCLE_MAX_FALSE_POSITIVE_RATE` | 0.05 |
| `AGENT6_LIFECYCLE_MIN_F1_IMPROVEMENT` | 0.005 versus the champion |
| `AGENT6_LIFECYCLE_AUTO_PROMOTE` | `false`; requires calibrated policy and supported real guard evidence |
| `AGENT6_LIFECYCLE_POLICY_CALIBRATED` | `false`; setting it is an explicit operational assertion |
| `AGENT6_LIFECYCLE_REVIEW_EXPIRY_HOURS` | 48; exact-candidate approvals expire |

Calibration should reflect fraud costs, acceptable missed-fraud rate, traffic mix, and representative independent outcomes. Merely changing a flag does not establish those facts. Missing labels do not block uptime, latency, input or drift checks.

## Application database and truth feed

`artifacts/application.sqlite3` contains prediction payloads, transaction features, operational records, and immutable resolved outcomes. `agent6_business_outcomes_v1` is the read interface for the outcome connector. Agent 6's SQLite reader is read-only. Its own state, evidence, assurance, lifecycle, and MLflow databases live separately under `artifacts/agent6/`.

The original API accepts trusted outcomes at `POST http://127.0.0.1:8021/business/outcomes`, with `Authorization: Bearer <outcome service credential>`. The launcher gives the producer the matching outcome credential through its process environment. A payment/investigation service should send:

```json
{
  "event_id": "c2aa8cee-6e98-4ca3-ac1e-50c1e3caa9ea",
  "observation_id": "d16774bb-2a2b-4f24-9d89-c26318c83dbf",
  "model_version": "artifact-<actual artifact SHA256 from the prediction response>",
  "actual": 1,
  "evidence_ref": "chargeback-case-1234",
  "occurred_at": "2026-10-31T12:00:00Z"
}
```

Use real IDs and independently established results: `0` means legitimate, `1` means fraud. Prediction outputs, LLM guesses, and an unverified absence of a chargeback are not confirmations. Repeated identical events are idempotent; conflicting outcomes are rejected. Outcomes cannot be reassigned to another model or scope. The database provides infrastructure for this feed; an external business service must supply the facts.

An existing business database can instead be configured using `AGENT6_MONITOR_BUSINESS_DATABASE_URL`, a typed column mapping and table/view name. The generic connector supports SQLite and optional PostgreSQL (`pip install ".[postgres]"`). SQL identifiers are validated and values parameterized. No database password belongs in documentation or chat.

## Deployment-agent contract

Agent 6 exposes this contract so deployment ownership stays with the team's deployment agent. It does not import or modify that agent.

1. Poll `GET /api/integrations/deployments` on port 8026, using the **integration** bearer credential, not the operator or business credential.
2. Handle `MODEL_DEPLOYMENT_REQUEST` or `MODEL_ROLLBACK_REQUEST` idempotently by request ID. A deployment request contains `candidate_sha256`, `evaluation_ref`, `previous_model_version`, exact scope, expiry, and active-version preconditions.
3. Obtain the artifact from the declared `shared-agent6-volume` path relative to `artifacts/agent6/`. Resolve only within the shared candidate directory, reject symlink escapes, and recompute the same directory digest from `assurance.worker.artifact_digest`.
4. Check expiry and compare the actual active model with `preconditions.active_model_version`. Verify candidate identity and evaluation before loading. Refuse a stale or mismatched request.
5. Perform your deployment agent's atomic/canary rollout, health checks, and independent post-deployment verification. Preserve the previous artifact for restoration.
6. Report `POST /api/integrations/deployments/{original_job_id}/result` with the integration credential:

```json
{
  "action": "DEPLOY",
  "status": "SUCCEEDED",
  "candidate_sha256": "<exact 64-character candidate digest>",
  "model_version": "artifact-<same digest>",
  "evidence_ref": "deployment-check-123"
}
```

For a rollback acknowledgment, use `action: ROLLBACK` and the restored previous version. A failed deployment acknowledgment queues restoration; a regression detected after confirmed deployment also queues restoration. An acknowledgment cannot prove a rollout unless the consumer performed it. Exact repeated callbacks are idempotent; conflicting state/version/hash callbacks return 409. No fabricated acknowledgments should be sent just to make the dashboard say `DEPLOYED`.

**Current external dependency:** the consumer is not wired in this branch. Requests remain durable and visible until consumed or expired. The other team's deployment branch must implement this contract and share access to candidate artifacts.

## Exceptional human review

Dashboard lifecycle jobs expose pending promotion reviews. Approve/reject using `POST /api/lifecycle/{job_id}/review` with the operator credential, an exact `candidate_sha256`, a decision `APPROVE`/`REJECT`, and an audit note. Approval rechecks the immutable evaluation and current artifact digest; the review is one-time and expires. Synthetic verification candidates cannot authorize deployment, even through review.

Reviews are for consequential policy/evidence exceptions. Insufficient labels produce a collection/waiting status, not fabricated results. Training failures retain the serving champion and visible failure evidence. An advisor answer never substitutes for a required guard or approval.

## Container option

```powershell
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle init
docker compose --env-file agent6_mlops_lifecycle/.env -f compose.agent6.yaml config --quiet
docker compose --env-file agent6_mlops_lifecycle/.env -f compose.agent6.yaml up --build -d
```

Stop the native stack first because the default ports overlap. The compose stack uses separate persistent state under `artifacts/agent6-container`, a read-only `Data` mount, and explicit internal DNS allowlisting. Keep that directory writable by the container's non-root account. It does not mount native environment files into images. Use the reverse proxy/identity platform appropriate to your deployment before exposing these local services externally.

Compose syntax was validated locally; the Docker engine was unavailable, so image builds and container execution remain unverified. Native processes and actual model execution were verified instead.

## Operational limits and recovery

- Logs: `artifacts/agent6/{api,streamlit,monitor}.log`; candidate worker logs and evidence identify the failed job.
- The supervisor refuses occupied ports and only stops its own child process trees. `agent6 stop` requests a graceful shutdown.
- An interrupted training job is marked failed on restart. Identical evidence is not silently retrained. A model cooldown, daily budget, and pending-job gate bound work.
- SQLite polling is suitable for this local integration. Window reads and training snapshots are bounded; recent verified training evidence is capped at 1,000 rows. Large production deployments need an explicit retention/archive policy and transport/worker scaling design. No silent pruning of audit evidence is implemented.
- Back up the application and Agent 6 SQLite files using SQLite's backup API or during a coordinated stop; include candidate artifacts, MLflow data, and the private configuration in access-controlled backups.
- Load only trusted local serialized artifacts. Existing pickle/joblib models are executable objects, not untrusted uploads.
- Rotate credentials through the private configuration and restart the affected services. Keep `.env`, SQLite files, candidates, logs and verification inputs out of Git and published test artifacts.

## Automatic transaction feed and richer fraud reports

See [Fraud evaluation](fraud-evaluation.md) for final business events, transaction-ID mapping, probability capture, cost configuration and subgroup support. The normal launcher respects a configured external business database URL; the local resolved-outcome view remains the default. Costs stay unknown until both error costs are explicitly configured.
