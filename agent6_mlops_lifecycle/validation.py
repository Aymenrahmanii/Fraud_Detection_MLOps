"""Real saved-model and autonomous-training acceptance, confined to isolated evidence."""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from agent6_mlops_lifecycle.assurance.bootstrap import configure_fraud_benchmark
from agent6_mlops_lifecycle.assurance.models import BenchmarkDataset, BenchmarkRegistry
from agent6_mlops_lifecycle.assurance.store import AssuranceStore
from agent6_mlops_lifecycle.assurance.worker import artifact_digest
from agent6_mlops_lifecycle.monitoring.models import MonitorScope
from agent6_mlops_lifecycle.monitoring.settings import ROOT
from agent6_mlops_lifecycle.runner import stop_child


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    data = ROOT / "artifacts/agent6/validation" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    data.mkdir(parents=True)
    scope = MonitorScope(tenant_id="verification", project_id="fraud-agent6-validation")
    benchmark = data / "assurance.sqlite3"
    registry_path = configure_fraud_benchmark(
        ROOT, benchmark, data / "benchmark-registry.json", scope, include_streamlit=True
    )
    registry = BenchmarkRegistry.model_validate_json(registry_path.read_bytes())
    dataset = BenchmarkDataset.model_validate(
        AssuranceStore(benchmark, scope).get("dataset", registry.dataset_id)
    )
    api_port, monitor_port = free_port(), free_port()
    while api_port == monitor_port:
        monitor_port = free_port()
    api_url, monitor_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{monitor_port}"
    operator, outcomes = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "MLFLOW_DISABLE_AGENT_HINT": "1",
            "AGENT6_PREDICTION_DATABASE": str(data / "application.sqlite3"),
            "AGENT6_TENANT_ID": scope.tenant_id,
            "AGENT6_PROJECT_ID": scope.project_id,
            "AGENT6_MODEL_ID": scope.model_id,
            "AGENT6_OUTCOME_SERVICE_TOKEN": outcomes,
            "AGENT6_MONITOR_TOKEN": operator,
            "AGENT6_MONITOR_OUTCOME_TOKEN": outcomes,
            "AGENT6_MONITOR_TENANT_ID": scope.tenant_id,
            "AGENT6_MONITOR_PROJECT_ID": scope.project_id,
            "AGENT6_MONITOR_MODEL_ID": scope.model_id,
            "AGENT6_MONITOR_SOURCE_DATABASE": str(data / "application.sqlite3"),
            "AGENT6_MONITOR_STATE_DATABASE": str(data / "state.sqlite3"),
            "AGENT6_MONITOR_EVIDENCE_DATABASE": str(data / "evidence.sqlite3"),
            "AGENT6_MONITOR_BENCHMARK_DATABASE": str(benchmark),
            "AGENT6_MONITOR_BENCHMARK_REGISTRY": str(registry_path),
            "AGENT6_MONITOR_BENCHMARK_PYTHON": sys.executable,
            "AGENT6_MONITOR_PREDICTION_URL": api_url + "/predict",
            "AGENT6_MONITOR_APPLICATION_HEALTH_URL": "",
            "AGENT6_MONITOR_INTERVAL_SECONDS": "0.5",
            "AGENT6_MONITOR_BASELINE_WARMUP_SECONDS": "0",
            "AGENT6_MONITOR_MIN_QUALITY_SCORE": "0.99",
            "AGENT6_MONITOR_ADVISOR_ENABLED": "false",
            "AGENT6_MONITOR_BUSINESS_DATABASE_URL": "sqlite:///"
            + str(data / "application.sqlite3"),
            "AGENT6_MONITOR_BUSINESS_FEED_MODE": "observation",
            "AGENT6_MONITOR_BUSINESS_OUTCOME_TABLE": "agent6_business_outcomes_v1",
            "AGENT6_MONITOR_BUSINESS_COLUMNS": "{}",
            "AGENT6_BUSINESS_SOURCE_IDS": "business-service",
            "AGENT6_MONITOR_OUTCOME_SOURCE_ID": "ISOLATED-HISTORICAL-LABEL-REPLAY",
            "AGENT6_LIFECYCLE_ENABLED": "true",
            "AGENT6_LIFECYCLE_INTEGRATION_TOKEN": secrets.token_urlsafe(32),
            "AGENT6_LIFECYCLE_INTERVAL_SECONDS": "1",
            "AGENT6_LIFECYCLE_ALLOW_VERIFICATION_JOBS": "true",
            "AGENT6_LIFECYCLE_AUTO_PROMOTE": "false",
            "AGENT6_LIFECYCLE_POLICY_CALIBRATED": "false",
            "AGENT6_LIFECYCLE_MODEL_PYTHON": sys.executable,
        }
    )
    children = []
    monitor = None
    try:
        with ExitStack() as stack:
            for name, command, url in [
                (
                    "api",
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "uvicorn",
                        "API.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(api_port),
                        "--no-access-log",
                    ],
                    api_url,
                ),
                (
                    "monitor",
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "agent6_mlops_lifecycle.cli.monitor",
                        "--port",
                        str(monitor_port),
                    ],
                    monitor_url,
                ),
            ]:
                log = stack.enter_context((data / (name + ".log")).open("wb"))
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                children.append(process)
                deadline = time.monotonic() + 90
                with httpx.Client(timeout=2, trust_env=False) as client:
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(
                                "Acceptance service exited; inspect its isolated log"
                            )
                        try:
                            if client.get(url + "/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError("Acceptance startup timed out")
                        time.sleep(0.2)
            monitor = httpx.Client(
                base_url=monitor_url,
                headers={"Authorization": "Bearer " + operator},
                timeout=30,
                trust_env=False,
            )
            stack.callback(monitor.close)
            with httpx.Client(base_url=api_url, timeout=30, trust_env=False) as api:
                events = []
                for row in dataset.rows:
                    transaction_id = "verification-" + str(uuid4())
                    response = api.post(
                        "/predict", json=row.inputs, headers={"X-Transaction-ID": transaction_id}
                    )
                    response.raise_for_status()
                    api_version = response.headers["X-Model-Version"]
                    assert 0 <= float(response.headers["X-Fraud-Probability"]) <= 1
                    events.append(
                        {
                            "event_id": str(uuid4()),
                            "transaction_id": transaction_id,
                            "source_id": "business-service",
                            "status": "FRAUD_CONFIRMED" if row.actual else "LEGITIMATE_CONFIRMED",
                            "verification_status": "VERIFIED",
                            "evidence_ref": "ISOLATED-HISTORICAL-DATASET:" + registry.dataset_id,
                            "occurred_at": datetime.now(UTC).isoformat(),
                        }
                    )
                for event in events:
                    api.post(
                        "/business/events",
                        json=event,
                        headers={"Authorization": "Bearer " + outcomes},
                    ).raise_for_status()
                assert (
                    api.post(
                        "/business/events",
                        json=events[0],
                        headers={"Authorization": "Bearer " + outcomes},
                    ).status_code
                    == 200
                )
            deadline = time.monotonic() + 360
            while True:
                response = monitor.get("/api/status")
                response.raise_for_status()
                status = response.json()
                jobs = status["lifecycle"]["jobs"]
                job = next(
                    (
                        row
                        for row in jobs
                        if row["status"] in ("REJECTED", "AWAITING_REVIEW", "FAILED")
                    ),
                    None,
                )
                if job:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Automatic candidate workflow did not finish")
                time.sleep(0.5)
            assert job["status"] != "FAILED", job
            fraud_report = status["reports"][0]["fraud_evaluation"]
            assert fraud_report["metrics"]["samples"] == len(events)
            assert fraud_report["probabilities"]["status"] == "ASSESSED"
            assert fraud_report["probabilities"]["samples"] == len(events)
            assert fraud_report["slices"]
            evaluation = json.loads(
                (Path(job["candidate_path"]).parent / "evaluation.json").read_text(encoding="utf-8")
            )
            assert artifact_digest(Path(job["candidate_path"])) == job["candidate_sha256"]
            splits = json.loads(
                (Path(job["candidate_path"]).parent / "split-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            fit, validation, guard = (
                set(splits[name])
                for name in (
                    "fit_feature_sha256",
                    "validation_feature_sha256",
                    "excluded_guard_feature_sha256",
                )
            )
            assert not fit & validation and not fit & guard and not validation & guard
            assert evaluation["guard_origin"] == "ISOLATED_VERIFICATION_FIXTURES"
            # Load the produced candidate artifact again and verify measured benchmark F1.
            import mlflow.pyfunc
            import pandas as pd
            from sklearn.metrics import f1_score

            candidate = mlflow.pyfunc.load_model(job["candidate_path"])
            predicted = candidate.predict(pd.DataFrame([row.inputs for row in dataset.rows]))
            assert (
                abs(
                    f1_score([row.actual for row in dataset.rows], predicted)
                    - evaluation["historical"]["f1"]
                )
                < 1e-12
            )
            app_code = """from streamlit.testing.v1 import AppTest
app=AppTest.from_file('app.py',default_timeout=60).run()
assert not app.exception,[error.message for error in app.exception]
[b for b in app.button if b.label=='Predict Fraud'][0].click().run()
assert not app.exception,[error.message for error in app.exception]
assert any('Monitoring observation:' in c.value for c in app.caption)
"""
            ui_log = stack.enter_context((data / "streamlit.log").open("wb"))
            subprocess.run(
                [sys.executable, "-B", "-c", app_code],
                cwd=ROOT,
                env=env,
                stdout=ui_log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=90,
            )
            deadline = time.monotonic() + 20
            while True:
                status = monitor.get("/api/status").json()
                if len(status["worker"]["operations"]) == 2:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Streamlit artifact was not collected independently")
                time.sleep(0.3)
            result = {
                "passed": True,
                "verification_only": True,
                "fixture_outcomes": (
                    "Historical project labels replayed into isolated storage; "
                    "not production confirmations"
                ),
                "strict_health_threshold_for_trigger": 0.99,
                "saved_api_model_executed": True,
                "original_streamlit_model_executed": True,
                "historical_predictions": len(events),
                "manual_reviews": 0,
                "automatic_training_triggered": True,
                "transaction_business_feed_verified": True,
                "api_probabilities_captured": len(events),
                "streamlit_probabilities_captured": any(
                    row["model_version"] != api_version
                    and row["positive_probability"] is not None
                    and row["decision_threshold"] is not None
                    for row in monitor.get("/api/predictions?limit=100").json()
                ),
                "fraud_risk_evaluation": fraud_report,
                "candidate_reloaded_and_rechecked": True,
                "fit_validation_guard_disjoint": True,
                "candidate_state": job["status"],
                "candidate_sha256": job["candidate_sha256"],
                "historical_candidate_metrics": evaluation["historical"],
                "live_fixture_candidate_metrics": evaluation["candidate_live"],
                "registered_version": job.get("registered_version"),
                "independent_artifact_streams": len(status["worker"]["operations"]),
                "production_deployment_performed": False,
                "external_llm_calls": 0,
                "data_directory": str(data),
            }
            (data / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
            monitor.post("/api/admin/stop-worker").raise_for_status()
    finally:
        for child in reversed(children):
            stop_child(child)
