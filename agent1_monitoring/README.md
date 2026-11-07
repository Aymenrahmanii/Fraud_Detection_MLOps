# Agent 1 — Monitoring & Anomaly Detection

Watches the services continuously and tells the orchestrator **what** is wrong, **where**, **when**,
how **severe** it is and how **confident** it is. It never gives the cause (Agent 2) and never
repairs anything (Agent 4). After a repair, it verifies independently that the service recovered.

It watches the fraud API from the outside, through Prometheus and Loki: the API code is not modified.

## How it works

Every 30 seconds, for each watched service:

| Layer | Role | Code |
|---|---|---|
| 1. Collection | Metrics from Prometheus (PromQL), logs from Loki (LogQL) | `collectors/` |
| 2. Detection | Availability, OOM, SLO burn rate, statistics, logs (Drain3); optional Chronos and Gemini | `detectors/`, `labelers/` |
| 3. Grouping | Symptoms become one incident per service and category | `grouping.py`, `incident_store.py` |
| 4. Reasoning | Which service is the probable origin (dependency graph, noisy-OR) | `reasoning.py` |
| 5. Decision | Alert, wait or collect more; no repeated alert | `decision.py`, `memory.py`, `feedback.py` |
| 6. Emission | `monitoring.anomaly.detected` event, v1 contract | `events.py` |
| 7. Verification | On `deployment.action.completed`: measure, then `monitoring.recovery.verified` | `recovery.py`, `inbox.py` |

Severity gives the priority: CRITICAL → P0, HIGH → P1, MEDIUM → P2.

Thresholds and watched services are in `configs/monitoring.yaml`, call dependencies in
`configs/dependencies.yaml`: another service needs configuration, not code (`mode: app` with
configurable metric names, or `mode: container` measured by cAdvisor).

## Install

Run every command from the repository root.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r agent1_monitoring\requirements.txt
```

Optional: to label unknown log messages with Gemini, create `agent1_monitoring\.env` (ignored by
Git) with `PIDS_GEMINI_API_KEY=<key>`. The agent works without it.

## Run

```powershell
docker compose -f compose.agent1.yaml --profile observability --profile fraud up -d fraud-api prometheus grafana cadvisor loki promtail
.venv\Scripts\python.exe agent1_monitoring\experiments\load\fraud_traffic.py --rps 2
.venv\Scripts\python.exe -m agent1_monitoring --verbose
```

- Grafana: <http://localhost:3000> (choose the service at the top).
- Events for the orchestrator are written to `agent1_monitoring\runs\agent1-events.jsonl`, one JSON
  envelope per line, until Kafka is connected.
- Start the agent about 5 minutes after the API: first requests are slow while the model warms up,
  and a recent restart is reported as a `readiness` incident.

The default configuration also watches the test service `payment-service`. Start it
(`target-app` in the command above) or remove it from `configs/monitoring.yaml`, otherwise the agent
correctly reports it as down.

## Test scenarios

| Scenario | Command | Duration |
|---|---|---|
| Fraud API: outage, recovery check, regression check | `python -m agent1_monitoring.experiments.checks.fraud_scenario` | ~12 min |
| Origin of a fault across test services | `python -m agent1_monitoring.experiments.checks.reasoning_scenario` | ~1 h 50 |

Verify a repair by hand:

```powershell
.venv\Scripts\python.exe -m agent1_monitoring.inbox event agent1_monitoring\experiments\checks\fraud_action_completed.json --window 120 --no-forecast
```

Give feedback on an alert: `python -m agent1_monitoring.feedback list`, then `vraie <id>` or
`fausse <id>`.

Measured results and known limits: [docs/Agent1_Resultats_tests.md](docs/Agent1_Resultats_tests.md).

## Link with Agent 2

Agent 2 reads the anomaly events directly. Its runner rejects a file containing a
`monitoring.recovery.verified` message, so keep only the anomalies first:

```powershell
Select-String -Path agent1_monitoring\runs\agent1-events.jsonl -Pattern '"monitoring.anomaly.detected"' | ForEach-Object Line | Set-Content agent1_monitoring\runs\anomalies.jsonl
.venv\Scripts\python.exe -m agent2_diagnosis.agent1_runner agent1_monitoring\runs\anomalies.jsonl --output-dir out
```

## Checks

```powershell
.venv\Scripts\python.exe -m pytest -q agent1_monitoring\tests
.venv\Scripts\python.exe -m ruff check agent1_monitoring
.venv\Scripts\python.exe -m mypy agent1_monitoring --exclude "tests|experiments" --ignore-missing-imports
```

## Known limits

- The list of watched services is declared in the configuration; there is no automatic discovery.
- Kafka is not connected: events go to a JSONL file and a repair is announced through `inbox.py`.
- `ContextAppender`, Kubernetes events and traces are not connected: only Docker containers are
  covered on the local bench.
- The local bench keeps the API's prediction log in memory (`tmpfs` in `compose.agent1.yaml`):
  on a loaded laptop its disk writes stall. Slow bursts remain and are reported by the agent.
- A planned restart counts as a `readiness` symptom for 5 minutes (no new alert is sent).
- In container mode (a service without `/metrics`), restarts are not seen; on Docker Desktop/WSL2 the
  kernel OOM counter stays at 0, so OOM kills need a service exposing `/metrics`.
- The two API bugs found by this agent are fixed in `main`: error detection is now tested with the
  test service (`/chaos/errors`), and `fraud_traffic.py --buggy-share` is a regression check.
