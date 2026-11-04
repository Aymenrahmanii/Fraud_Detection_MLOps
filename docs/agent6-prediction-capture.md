# Agent 6 serving prediction capture

The running fraud API now saves a prediction record after every successful
`POST /predict`. The existing JSON body remains `{"prediction": 0}` or
`{"prediction": 1}`. Response headers add `X-Observation-ID` and `X-Model-Version`.
The application consuming the API should retain the observation ID so a trusted
outcome source can later attach the confirmed answer to the same prediction.

## What is saved

Each record follows Agent 6 PredictionRecord v1: tenant/project/model/version,
observation ID, source artifact digest, binary prediction, UTC observation time and
UTC availability time. The model version is derived from the actual local serving
artifact, not a version supplied by an HTTP caller. The artifact is fingerprinted
before and after loading; changed artifacts fail startup.

The log contains no raw request features, customer identifiers, labels or invented
probabilities. Outcome collection is a separate remaining integration. Prediction
recording alone cannot establish accuracy or F1.

The local SQLite file defaults to `artifacts/agent6/predictions.sqlite3` relative to
this fraud repository. Transactions, full synchronization and independent SQLite
connections support concurrent workers on one local host. A successful HTTP response
is sent only after the insert commits. Recording failure returns HTTP 503 with a
safe message; a request may have incurred inference work before that failure.
Invalid requests and failed inference do not create successful prediction records.
Retries are new prediction attempts with new IDs; this is not an exactly-once API.

`/health` remains a process liveness endpoint; it does not prove the log is writable.
There is no public endpoint for rewriting records or submitting unauthenticated
labels. Local file access and trusted environment configuration define the current
boundary. Authentication, retention, backup, readiness monitoring and multi-host
transport remain deployment work.

## Configuration

Copy `API/telemetry/.env.example` to `API/telemetry/.env` if local overrides are
needed. The producer reads only this file, and process environment variables take
precedence. It does not read or modify the automation repository's Agent 6 `.env`.
Restart the API after changing producer configuration. Example defaults:

~~~dotenv
AGENT6_PREDICTION_DATABASE=artifacts/agent6/predictions.sqlite3
AGENT6_TENANT_ID=local-fraud
AGENT6_PROJECT_ID=fraud-detection-serving
AGENT6_MODEL_ID=fraud-api-pipeline
~~~

The database and producer-local `.env` are ignored by Git. Docker excludes local
`.env` files; configure the producer through runtime variables and mount persistent
storage when running a container. Install dependencies from this project's
requirements in its model-compatible environment; python-dotenv is now explicit.

## Start and use the API

From the automation repository root, using its prepared model environment:

~~~powershell
.\artifacts\agent6-fraud-project\model-env\Scripts\python.exe -m uvicorn API.main:app --app-dir ../Fraud_Detection_MLOps --host 127.0.0.1 --port 8001
~~~

Use the existing `/docs` page or `/predict` client. Each successful request records
one event and returns its joinable observation ID in a header. Existing clients
that read only the `prediction` field remain compatible. Input features are used
by the model and are not copied to telemetry.

## Export a snapshot

From this fraud repository root, with its compatible interpreter:

~~~powershell
python -m API.telemetry export --lookback-minutes 60 --output-dir artifacts/agent6/snapshots/window-001
~~~

The snapshot contains `predictions.jsonl`, an empty `outcomes.jsonl`, a matching
`manifest.json`, `sources.json`, and `capture-metadata.json`. Reads use a bounded
read-only SQLite transaction; stored digests/schema/scope are checked. Snapshots
never replace an existing export. Choose a new output directory for each export.
A window is capped at 20,000 predictions and 16 MiB of JSONL; use smaller windows
for larger streams. Digests detect unexpected modifications, not a malicious actor
with write access to both data and its digest.

An export selects one model version; mixed-version windows require an explicit
`--model-version artifact-<full-sha256>`. Optional `--window-start`, `--window-end`
and `--as-of` accept timezone-aware ISO timestamps. Outcomes remain empty until a
trusted source supplies them. Example metric thresholds are illustrative and need
project-owner calibration before operational decisions.

Agent 6 registers `manifest.json`, then reads `sources.json` through its existing
JSONL ingestion path. Until independently confirmed outcomes are provided, the
correct result is `INSUFFICIENT_EVIDENCE`, with pending observation IDs and zero
outcome coverage. Even after outcomes arrive, the example policy requires at least
100 labeled observations, positive/negative support, and sufficient coverage.

## Test the complete capture path

From the automation repository root:

~~~powershell
.\.venv\Scripts\python.exe scripts/test_agent6_live_capture.py
~~~

The command starts a temporary API on localhost using the real serving model,
sends three requests using CSV feature rows, verifies response IDs against the
persisted records, exports JSONL and ingests it into Agent 6. It supplies no CSV
labels and stops its own API process after the test. Output is saved under the
automation repository's `artifacts/agent6-live-capture/<run>/` directory.

This completes serving prediction capture. Trusted outcome submission, recurring
exports/ingestion, orchestrator review and deployment handoff remain separate work.
