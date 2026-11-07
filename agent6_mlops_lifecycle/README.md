# Agent 6 — MLOps lifecycle

Agent 6 observes the existing fraud API and Streamlit models, checks operational health and model quality, and prepares evaluated replacement candidates automatically when verified outcomes show degradation. It is self-contained in this repository; the former automation repository is not needed at runtime.

This integration is based on shared `main` commit `c5f04e1`, on local branch `Agent6/main`. Agent 3 and Agent 5 implementation directories are unchanged. Small application hooks record actual inference and independently supplied outcomes.

## Start locally

Use Python **3.12**. Run these commands from the fraud repository root. The model compatibility constraints preserve the versions needed by its existing serialized artifacts.

```powershell
py -3.12 -m venv .venv-agent6
.\.venv-agent6\Scripts\python.exe -m pip install -r requirements.txt -e ".[dev]"
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle init
```

`init` creates Agent 6's private `agent6_mlops_lifecycle/.env`, generates distinct credentials, and imports the existing labeled holdout into its benchmark catalog. It sends no transactions and creates no live fraud outcomes. Add `GROQ_API_KEY` to this file if you want the optional advisor, then start:

```powershell
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle run --open-browser
```

| Service | Local address | Purpose |
| --- | --- | --- |
| Original fraud application | http://127.0.0.1:8521 | Use the existing Streamlit model |
| Original fraud API | http://127.0.0.1:8021/docs | Inference and trusted business outcomes |
| Agent 6 dashboard | http://127.0.0.1:8026 | Health, drift, quality, evidence, lifecycle jobs |
| Agent 6 metrics | http://127.0.0.1:8026/metrics | Bounded Prometheus metrics |

The browser option opens authenticated access without printing a credential. For another tab, use `AGENT6_MONITOR_TOKEN` from Agent 6's `.env`; the Groq key is a different credential. Tokens stay in page memory. The supervisor owns only the processes it launches, fails on occupied ports, and shuts down its children if one exits.

```powershell
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle doctor
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle status
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle stop
```

Restart after changing `.env`. `run --api-only` omits Streamlit; `--api-port`, `--port` and `--app-port` select alternative local ports. The installed `agent6` command exposes the same commands. Installed-wheel usage outside this checkout needs `AGENT6_PROJECT_ROOT` pointing at the fraud repository; `AGENT6_ENV_FILE` can select a separate private configuration file.

For reproducibility, `constraints-tested.txt` records the native Python 3.12 package versions used during verification. Apply it with `pip install -c agent6_mlops_lifecycle/constraints-tested.txt -r requirements.txt -e ".[dev]"`. This is a tested version snapshot, not a cross-platform dependency lock or a guarantee about unrelated future package updates.

## What runs automatically

1. API and Streamlit record real predictions, business transaction IDs, actual fraud probabilities and decision thresholds, artifact identities, feature evidence, inference duration, and failures in the application database.
2. Agent 6 polls every five seconds, checks API/UI reachability, latency, errors, missing/invalid inputs, label availability, and feature/prediction distribution shift.
3. A reference is captured after the configured warm-up and sufficient observations. PSI subsequently compares distributions using the same buckets. Drift alone does not prove incorrect predictions.
4. Trusted business outcomes are joined to predictions by observation ID, scope, and exact model version. Precision, recall, F1, balanced accuracy, and support gates are computed by code. Final business events can arrive by authenticated webhook or a read-only transaction-event database feed; matching and delayed-delivery recovery are automatic. A separate fraud report measures missed fraud, false alarms, subgroup support, probability ranking/calibration, and explicitly configured error costs.
5. The existing saved API and Streamlit artifacts are independently benchmarked against the project's 300 labeled holdout rows. These historical scores are displayed separately from live correctness.
6. Measured live quality failure, enough verified outcomes, class support, cooldown, and budget permit automatic candidate training. Older production feature groups may augment the source dataset; recent groups and benchmark groups are excluded from fitting and threshold tuning.
7. The candidate is evaluated against historical and recent held-out outcomes, compared with the champion, saved with immutable evidence, and registered in local MLflow if it qualifies. Failed candidates are rejected automatically.
8. A qualified candidate receives an automatic deployment handoff only under an explicitly calibrated policy with supported production evidence and a positive improvement interval. Other consequential promotions receive an expiring, exact-candidate review. Routine monitoring and candidate preparation need no approval.
9. A separately authenticated deployment agent consumes durable deployment requests and acknowledges results. Measured regression after a confirmed deployment, or a failed deployment acknowledgment, creates a rollback request automatically.

## Completion boundaries

The monitoring, verified-outcome connector, benchmark scheduler, constrained Groq advisor, real training/evaluation/registration, conditional promotion policy, review handling, and deployment/rollback request contracts are implemented here.

**An actual deployment consumer is not connected in this branch.** Agent 6 emits durable requests; it does not replace the other team's deployment agent or silently switch a serving model. The deployment team must implement the documented consumer contract before unattended rollout can finish. Default promotion remains uncalibrated and review-bound, while routine work runs autonomously.

**Business truth still needs a real upstream source.** The new application database stores independently verified outcomes; it cannot discover that a transaction was actually fraudulent by itself. Until a payment, chargeback, investigation, or equivalent service supplies confirmations, live quality correctly remains `WAITING_FOR_LABELS`; operational monitoring, drift, and historical benchmarks continue. An LLM cannot create ground truth.

This release targets the two binary fraud artifacts in this repository. The reusable evaluation modules also support configured classification, regression, and externally supplied metric/LLM-judge policies. They do not make the fraud-specific retrainer universal for every model family.

## Documentation and verification

- [Dashboard views and browser verification](docs/dashboard.md)
- [Automatic business feed, fraud metrics and probabilities](docs/fraud-evaluation.md)
- [Architecture and file responsibilities](docs/architecture.md)
- [Configuration, data, operations and deployment-agent contract](docs/operations.md)
- [Acceptance scenarios and honest interpretation of results](docs/verification.md)
- [Integration change report and publication verification](docs/integration-report.md)

Run the actual isolated acceptance workflow with:

```powershell
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle validate
```

It uses the real saved models and trainer, but replays historical outcomes into separate verification storage. It neither contaminates live records nor authorizes deployment. See the verification guide for full tests, result files, and limitations.
