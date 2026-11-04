# Automatic business truth, fraud risk and probability evaluation

This extension implements the requested outcome feed, fraud-specific evaluation and probability capture. Deployment-agent integration remains a separate team task.

## 1. Automatic verified business confirmations

Every new API prediction receives a business transaction ID. Supply your actual business ID using `X-Transaction-ID` on `POST /predict`; otherwise the API generates one and returns it in that header. Streamlit has an optional business-ID field and generates/displays an ID when it is blank. The existing observation/model headers and prediction JSON response are preserved.

The business application should retain that ID with the original transaction and send a final verified result to `POST /business/events` using the distinct outcome-service bearer credential. It does not need to discover model versions or submit a separate outcome for each model observation:

```json
{
  "event_id": "c2aa8cee-6e98-4ca3-ac1e-50c1e3caa9ea",
  "transaction_id": "payment-1234",
  "source_id": "business-service",
  "status": "FRAUD_CONFIRMED",
  "verification_status": "VERIFIED",
  "evidence_ref": "independent-investigation-1234",
  "occurred_at": "2026-11-01T12:00:00Z"
}
```

Use `LEGITIMATE_CONFIRMED` for a positively established legitimate outcome. Both statuses require independent final evidence from an allowlisted source. Payment success, a model prediction, an LLM answer, a pending investigation, or an unverified lack of a chargeback is not accepted as truth. The timestamp must be after the original prediction and cannot be in the future.

The application stores the event durably, correlates its business ID to the exact scoped model observations, and creates immutable resolved outcomes. Agent 6 automatically polls those records. No operator needs to review each transaction. Exact webhook retries are idempotent. Conflicting truth, reused event IDs, or a transaction rebound to different input values are rejected without changing existing evidence. A final event that arrives before a delayed prediction record stays in the inbox; capture later reconciles it automatically, provided the original prediction time precedes confirmation. A prediction made after the answer was already known is not labeled from that earlier confirmation.

Numeric `0`/`0.0` representations from API coercion and Streamlit widgets correlate consistently. The original captured feature JSON is still hashed exactly into the prediction evidence. The same business transaction can be evaluated by both models, with separate artifact-linked outcomes. Repeated observations of the same business transaction within one model's fraud assessment are excluded from its support count.

### Existing application database instead of webhooks

The existing observation-ID feed remains supported. For a database containing final business transaction events, configure Agent 6's private `.env`:

```dotenv
AGENT6_MONITOR_BUSINESS_FEED_MODE=transaction
AGENT6_MONITOR_BUSINESS_DATABASE_URL=sqlite:///path/to/business.sqlite3
AGENT6_MONITOR_BUSINESS_OUTCOME_TABLE=verified_transaction_events
AGENT6_MONITOR_BUSINESS_SOURCE_IDS=["business-service"]
```

The view needs these columns: `sequence_id`, `event_id`, `transaction_id`, `source_id`, `status`, `verification_status`, `evidence_ref`, `occurred_at`, `tenant_id`, `project_id`, `model_id`. `sequence_id` must be an increasing integer. The scope must match the monitor. Column mappings use `AGENT6_MONITOR_BUSINESS_COLUMNS` as a JSON object; simple SQL identifiers are validated and data values are parameterized. SQLite is opened read-only; the optional PostgreSQL reader uses a read-only connection with a statement timeout. PostgreSQL requires the `postgres` package extra and a URL supplied privately.

The reader stores valid events in the local durable inbox before advancing its scoped checkpoint. An unmatched transaction does not block unrelated later valid events. A restart resumes safely. Invalid/conflicting truth stops that feed with a visible error and retains the checkpoint for investigation. Already durable local resolutions can still be consumed during an upstream outage. The normal launcher respects an explicitly configured external URL.

For webhook publishers, configure `AGENT6_BUSINESS_SOURCE_IDS` on the API; the native launcher forwards the monitor allowlist automatically. The compose API reads its own comma-separated source setting. Keep producer and reader allowlists consistent.

**Remaining external dependency:** this repository contains a fraud inference application, not a payment processor or chargeback authority. The adapters are operational, but actual business truth must come from a real source that posts events or writes the configured view. No labels are manufactured to make the dashboard appear complete.

## 2. Fraud-specific evaluation

Each scoped monitoring report now includes `fraud_evaluation`. It uses only attributed `SYSTEM` or independently verified `HUMAN` outcomes, preserves the observation cutoff, and excludes simulated outcomes from measured fraud quality. New predictions link the captured feature digest to the immutable prediction payload; tampered inputs cannot silently change a subgroup. Legacy records without that digest remain usable for class metrics but are counted as missing subgroup evidence.

The report includes:

- True/false positives and true/false negatives: detected fraud, false alarms, missed fraud, correctly allowed legitimate transactions.
- Fraud recall, missed-fraud rate, false-positive rate, precision and F1, with explicit undefined values where a denominator is zero.
- Wilson 95% intervals for recall, precision and false-positive rate. These describe sampling uncertainty under the usual sampling assumptions, not a probability that the model is correct.
- Diagnostic slices for payment method, category and account-age buckets by default, with configurable allowlisted slice features.
- Explicit support requirements. Sparse groups remain `INSUFFICIENT_EVIDENCE`; they are not promoted to trustworthy pass/fail results because of one fraud case.
- Overall recall/false-positive gates and optional cost/average-precision gates. A supported overall fraud failure emits `FRAUD_RISK` and can make an incident eligible for existing automatic candidate preparation, subject to its label, class, cooldown and budget gates.

Slice failures are diagnostic, rather than automatically changing a model based on multiple small-group comparisons. Inputs and business transaction IDs are not sent to the LLM by this extension. Dashboard rendering uses measured report fields directly.

### Business costs

Costs are deliberately unconfigured by default. Set both costs in Agent 6's `.env` when the business policy is known:

```dotenv
# Illustrative values only; replace with the agreed business policy.
AGENT6_FRAUD_FALSE_NEGATIVE_COST=100
AGENT6_FRAUD_FALSE_POSITIVE_COST=2
AGENT6_FRAUD_COST_UNIT=EUR
# Optional operational gate, after calibration:
# AGENT6_FRAUD_MAX_COST_PER_1000=1000
```

The calculation is `missed_fraud_count * false_negative_cost + false_alarm_count * false_positive_cost`. Cost per 1,000 scales this total by the labeled sample size. It is a constant-cost policy estimate, not observed monetary losses or transaction-amount-weighted fraud loss. A cost gate cannot be enabled while costs are unknown. The existing training recipe still tunes its threshold on independent validation F1; the new evaluation cost comparisons are advisory and do not silently alter training or serving thresholds.

## 3. Recorded probabilities and threshold analysis

API and Streamlit now record `positive_probability` and the actual `decision_threshold` alongside the binary prediction. API probability evidence comes from the trusted loaded native model, is checked against the authoritative class, and is exposed in `X-Fraud-Probability` / `X-Decision-Threshold`. Unsupported adapters leave probability evidence unavailable while ordinary inference remains usable. No `0`/`1` probabilities are invented from class labels. NaN, infinity, out-of-range scores and inconsistent threshold/class evidence cannot be persisted as valid probability records.

The evaluator computes probability quality on matched labeled observations that actually have probabilities:

- Average precision using tied-score groups, plus ROC AUC.
- Brier score (mean squared error of the fraud probabilities).
- Ten-bin calibration statistics and expected calibration error.
- Bounded precision–recall curve points and a 21-threshold comparison grid.
- A cost-minimizing advisory threshold only when costs and support are sufficient and recall/false-positive constraints are met.

Probability support defaults to at least 30 labeled scores, class support, and 90% probability coverage among the labeled observations. Unsupported/small subsets are clearly marked even when a descriptive point estimate can be calculated. Probabilities are estimates, not guaranteed correctness. Selectively available business labels can bias calibration, ranking, and all other quality metrics.

The dashboard adds a fraud-risk panel, diagnostic subgroup table, a precision–recall plot, calibration table, and threshold-comparison slider. The slider only explores the evaluation sample; it never applies a threshold. Real changes require separate validation and lifecycle policy checks. Old observations are not backfilled by rerunning a different model or guessing its original probability.

Prometheus exposes bounded missed-case, false-alarm, optional cost and supported probability-quality gauges. Runtime records, credentials and raw feature snapshots remain excluded from Git.

## Configuration, testing and restart

All policy defaults are typed in `fraud/models.py`, under the `AGENT6_FRAUD_` prefix. `.env.example` documents the main settings. The integration uses additive optional prediction fields and new application tables, so older prediction records remain readable.

```powershell
.\.venv-agent6\Scripts\python.exe -m pytest -q API/tests/test_business_events.py agent6_mlops_lifecycle/tests/unit/test_fraud_risk.py
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle validate
```

Acceptance executes the actual saved API/UI models, captures their probabilities, replays historical final business events into isolated verification storage, checks the new fraud report, and runs automatic candidate preparation. It makes no production confirmations or deployments and no external LLM calls. Separate tests cover database-feed recovery, replay conflicts, duplicate transactions, probabilities with ties, missing legacy scores, integrity failures and explicit cost calculations.

Restart the existing local stack to load the new producer and monitor code:

```powershell
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle stop
# Wait for the existing supervisor to stop, then:
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle run --open-browser
```
