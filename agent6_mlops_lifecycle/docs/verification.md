# Verification and demonstration scenarios

## Automated regression checks

Use the repository's Python 3.12 environment. Existing Agent 3 tests invoke developer executables, so place this environment's Scripts directory on `PATH` when running them on Windows.

```powershell
.\.venv-agent6\Scripts\python.exe -m pip install -r agent3_security_quality/requirements.txt -r agent5_resource_optimization/requirements.txt
$env:Path = (Join-Path (Get-Location).Path '.venv-agent6\Scripts') + [IO.Path]::PathSeparator + $env:Path
.\.venv-agent6\Scripts\python.exe -m pytest -q API/tests agent3_security_quality/tests agent5_resource_optimization/tests agent6_mlops_lifecycle/tests
.\.venv-agent6\Scripts\python.exe -m ruff check agent6_mlops_lifecycle
.\.venv-agent6\Scripts\python.exe -m mypy agent6_mlops_lifecycle
```

The November 4 publication check passed **468 tests**, with seven existing Pydantic deprecation warnings in the original API schema. Agent 6 strict type checking passed for **97 source files**. Ruff and the installed dependency compatibility check also passed. Warnings identify future schema migration work; they were not hidden or counted as failures. Test counts can increase as the implementation evolves.

## Real saved-model and autonomous-training acceptance

```powershell
.\.venv-agent6\Scripts\python.exe -m agent6_mlops_lifecycle validate
```

The command creates an isolated verification directory under `artifacts/agent6/validation/<timestamp>/`, chooses its own local ports, and starts its own application/monitor processes. It:

1. Sends all 300 historical holdout feature rows through the actual original API model.
2. Replays the corresponding known historical labels into the verification database, explicitly marked as verification fixtures.
3. Uses a deliberately strict 0.99 quality threshold to exercise the degraded-model trigger. This is test configuration, not a calibrated fraud requirement.
4. Waits for Agent 6 to train a real candidate automatically, save an MLflow model, evaluate it, and register it if qualified.
5. Checks the fit/validation/guard feature memberships are disjoint.
6. Loads the candidate from disk again and recomputes historical F1.
7. Runs the original Streamlit app using Streamlit's test harness and its actual saved pipeline, then verifies its artifact stream was collected separately from the API stream.
8. Writes `result.json`, retains local evidence/logs, and shuts down only its own processes.

This test makes no external LLM calls, requires no manual reviews, and performs no production deployment. A fixture-origin candidate is prohibited from deployment, including through review. Its `AWAITING_REVIEW` state exercises policy handling, not a real promotion opportunity.

The successful result proves that real model execution, telemetry, outcome joining, training, registration, serialization, and policy wiring work together. High scores on these historical fixtures do not establish accuracy on future business transactions. Independence of the original pre-existing saved models from the old holdout remains unverified. Agent 6's newly trained candidate excludes the guard feature groups explicitly.

## Scenarios to present

| Scenario | Action | Expected evidence |
| --- | --- | --- |
| Start without traffic | `run --open-browser` | API/UI probes and scheduled historical benchmarks run; no live predictions are invented |
| Real application traffic | Submit a real transaction using the original Streamlit page or API | A new observation and actual artifact identity appear automatically |
| No business confirmations | Leave the real outcome pending | Live precision/recall/F1 stay unknown; operational checks continue |
| Confirmed business results | A trusted business service posts actual outcomes | Connector joins IDs, shows coverage, and evaluates only after support gates pass |
| Automatic drift reference | Accumulate sufficient actual observations and warm-up | Baseline is captured without a review; later supported windows get PSI |
| Serving/input problems | Run a controlled isolated invalid-input or unavailable-service check | Error/input/availability evidence is shown without claiming accuracy degradation |
| Quality failure | Run `validate` to exercise a reproducible isolated failure | Automatic candidate training and real MLflow registration; no manual workflow needed |
| Weak replacement | Lifecycle regression tests supply a failing candidate | Candidate rejected, champion retained |
| Unsupported promotion | A qualified candidate lacks calibrated/supported real evidence | Expiring exact-candidate HITL request |
| Supported autonomous promotion | Contract tests supply calibrated policy and sufficient verified guard evidence | Durable deployment request, no person in the routine path |
| Deployment failure/regression | Authenticated contract test acknowledges failure or supplies deployed regression | Idempotent rollback request; restoration requires deployment-agent acknowledgment |
| Groq unavailable | Remove/disable the optional advisor in a separate configuration | Measured monitoring and lifecycle continue; advisor status explains unavailability |

Contract tests exercise fault, review, promotion and rollback conditions with controlled inputs. They should not be presented as completed live rollouts. The standalone native stack also verifies actual scheduled benchmark and Groq connectivity when its private provider key is configured.

## Release checks

`python -m build` creates the Agent 6 wheel and source archive. Inspect archive members before distributing: no actual `.env`, model binaries, SQLite databases, test snapshots, other agents, or former repository source should be present. The CI workflow runs tests, static checks, actual acceptance, and the package build. CI uploads only aggregate acceptance/evaluation JSON, not databases or raw transaction snapshots.

Before unattended production rollout: connect a real verified-outcome feed; calibrate business gates on representative independent data; implement the deployment consumer and post-rollout checks; verify container images on a working engine; configure identity, backup, retention and capacity for the target environment. These are explicit integration dependencies, not tasks an LLM can infer from predictions.

## November 1 fraud-risk extension

The upgraded acceptance command also checks actual probability headers on 300 API requests, correlation through final business transaction events, fraud confusion counts and probability quality, diagnostic slices, and probability capture from the original Streamlit model. See [Fraud evaluation](fraud-evaluation.md) for configuration and feed prerequisites. Historical confirmations are isolated fixtures; no business truth or deployment is simulated in the normal application database.

## Dashboard redesign verification

The redesign passed 468 backend regression tests and an optional real Edge browser suite covering 19 interaction/layout scenarios. Desktop and mobile screenshots use isolated UI fixtures, with no application API mutations. A separate read-only browser check also loaded the redesign against the running local monitor, with zero JavaScript errors. See [Dashboard guide](dashboard.md) for navigation, access and test commands.
