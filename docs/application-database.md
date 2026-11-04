# Application database and automatic Agent 6 monitoring

The API and Streamlit application now persist serving requests to
`artifacts/application.sqlite3`. SQLite requires no separate database server.
`transactions` contains actual app inputs and prediction identities;
`resolved_outcomes` contains only independently verified business events.
The `agent6_business_outcomes_v1` view exposes committed events for read-only
Agent 6 ingestion. Creating the database does not create or guess fraud labels.

The automation repository's launcher starts the original app/API and Agent 6 with
matching database and scope settings. From that repository:

~~~powershell
.\.venv\Scripts\python.exe scripts/run_fraud_monitor.py --with-streamlit --api-port 8011 --port 8012 --open-browser
~~~

Both inference paths record timing and bounded feature buckets automatically.
Transactions, successful predictions and telemetry are committed together.
Failed inference and API validation attempts are recorded without fake predictions.
No CSV export or operator review is required for ordinary monitoring.

A trusted business service may submit an independently verified outcome to
`POST /business/outcomes`, using `AGENT6_OUTCOME_SERVICE_TOKEN` from the producer's
runtime environment. The launcher supplies a distinct service credential from
Agent 6's own ignored `.env`. The endpoint accepts stable event/observation UUIDs,
exact model version, integer actual outcome 0/1, evidence reference and aware
occurrence time. It rejects unknown transactions, wrong scope/model, future times
and conflicting/reused evidence. Identical retries do not duplicate the outbox.

Agent 6 reads verified events automatically, persists its checkpoint and evaluates
quality when sufficient labels arrive. Until a real trusted outcome source is
connected, availability/latency/error/input/drift checks still run, while F1 and
other correctness scores remain unknown. Model predictions never become labels.

The database, journals, runtime MLflow state and environment keys are excluded from
Git and container build context. The original serving model artifacts remain in
their existing tracked locations. See the automation repository's
`src/agents/mlops_lifecycle/docs/live-monitoring.md` for settings, operational
limits, reference warm-up, delayed outcomes, tests and production next steps.
