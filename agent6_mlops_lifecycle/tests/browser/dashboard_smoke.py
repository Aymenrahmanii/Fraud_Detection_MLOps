"""Optional real-browser UI checks; synthetic fixtures never reach an application API.

Run after installing Playwright and its Chromium browser:
    python agent6_mlops_lifecycle/tests/browser/dashboard_smoke.py
Use --channel msedge to use an installed Edge browser on Windows.
"""

import argparse
import copy
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "agent6_mlops_lifecycle/monitoring/web"
TOKEN = "synthetic-browser-test-credential-only"
VERSION = "artifact-" + "a" * 64
OTHER = "artifact-" + "b" * 64


def fixtures():
    from agent6_mlops_lifecycle.fraud.metrics import confusion, probability_quality
    from agent6_mlops_lifecycle.fraud.models import FraudSettings

    now = datetime.now(UTC).isoformat()
    policy = FraudSettings(_env_file=None)
    metrics = confusion([(i % 2, i % 2) for i in range(30)], policy).model_dump(mode="json")
    probabilities = probability_quality(
        [(i % 2, 0.9 if i % 2 else 0.1) for i in range(30)], 30, policy
    ).model_dump(mode="json")
    evaluation = {
        "decision": "PASS",
        "sample_count": 30,
        "metrics": {
            f"binary.{name}.v1": 1 for name in ("precision", "recall", "f1", "balanced_accuracy")
        },
        "gate_results": [],
    }
    report = {
        "model_version": VERSION,
        "assessed_at": now,
        "schema_version": "1",
        "ingestion": {
            "summary": {
                "available_prediction_count": 30,
                "matched_count": 30,
                "outcome_coverage": 1,
                "pending_observation_ids": [],
            }
        },
        "evaluation": evaluation,
        "drift": {
            "status": "STABLE",
            "score": 0.01,
            "reference_samples": 100,
            "current_samples": 100,
        },
        "feature_drift": {"status": "STABLE"},
        "fraud_evaluation": {
            "status": "PASS",
            "policy": policy.model_dump(mode="json"),
            "metrics": metrics,
            "probabilities": probabilities,
            "outcome_coverage": 1,
            "duplicate_transactions_excluded": 0,
            "missing_feature_evidence": 0,
            "cost_unit": None,
            "slices": [
                {"feature": "Category", "bucket": "shopping", "metrics": metrics, "status": "PASS"}
            ],
        },
    }
    operations = {
        "status": "HEALTHY",
        "latency_p95_ms": 18.4,
        "failure_rate": 0,
        "input_issue_rate": 0,
        "attempts": 30,
        "failures": 0,
        "telemetry_coverage": 1,
        "reasons": [],
    }
    data = {
        "stale": False,
        "worker": {
            "state": "MONITORING",
            "polled_at": now,
            "active_versions": [VERSION],
            "services": {
                name: {"state": "UP", "checked_at": now}
                for name in ("fraud-api", "fraud-application")
            },
            "operations": {VERSION: operations},
            "outcome_feed": {"state": "CONNECTED", "last_sequence": 30},
        },
        "configuration": {
            "scope": {"tenant_id": "verification"},
            "application_url": "http://127.0.0.1:8521/",
            "demo_mode": False,
            "manual_review_enabled": False,
            "baseline_mode": "auto",
            "baseline_warmup_seconds": 60,
            "quality_window_seconds": 604800,
            "min_samples": 30,
            "min_quality_score": 0.8,
            "min_outcome_coverage": 0.5,
            "interval_seconds": 5,
            "min_distribution_samples": 100,
            "outcome_connector": {"enabled": True, "database_configured": True},
        },
        "reports": [report],
        "baselines": {VERSION: {}},
        "alerts": [],
        "lifecycle": {"enabled": True, "min_verified_labels": 100, "jobs": []},
        "assurance": {
            "benchmark_interval_seconds": 3600,
            "benchmarks": [
                {
                    "model_version": version,
                    "artifact_name": name,
                    "status": "COMPLETED",
                    "report_ref": "c" * 64,
                    "report": {
                        "evaluations": {"ALL": evaluation},
                        "label_counts": {"ALL": [15, 15]},
                        "assessed_at": now,
                        "label_source": "PROJECT_DATASET",
                        "training_independence": "UNVERIFIED",
                    },
                }
                for name, version in (("Fraud API", VERSION), ("Fraud Streamlit", OTHER))
            ],
            "advisor": {"status": "DISABLED"},
        },
    }
    rows = [
        {
            "observation_id": str(uuid4()),
            "observed_at": now,
            "model_version": VERSION,
            "predicted": i % 2,
            "actual": None if i == 0 else i % 2,
            "positive_probability": 0.9 if i % 2 else 0.1,
            "decision_threshold": 0.5,
            "outcome_kind": None if i == 0 else "SYSTEM",
        }
        for i in range(3)
    ]
    return data, rows


class Assets(BaseHTTPRequestHandler):
    def do_GET(self):
        names = {
            "/": ("index.html", "text/html"),
            "/assets/style.css": ("style.css", "text/css"),
            "/assets/app.js": ("app.js", "text/javascript"),
        }
        item = names.get(urlsplit(self.path).path)
        if not item:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", item[1] + "; charset=utf-8")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write((WEB / item[0]).read_bytes())

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    output = ROOT / "artifacts/agent6/ui-review"
    output.mkdir(parents=True, exist_ok=True)
    healthy, rows = fixtures()
    empty = copy.deepcopy(healthy)
    empty["worker"].update(state="WAITING_FOR_PREDICTIONS", active_versions=[], operations={})
    empty["reports"], empty["baselines"] = [], {}
    state = {"data": empty, "rows": [], "outage": False}
    mutations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Assets)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as browser_tool:
            browser = browser_tool.chromium.launch(channel=args.channel, headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1050}, device_scale_factor=1
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route(request):
                req = request.request
                if req.headers.get("authorization") != "Bearer " + TOKEN:
                    request.fulfill(status=401, json={"detail": "Test credential rejected"})
                elif state["outage"]:
                    request.fulfill(status=503, json={"detail": "Isolated outage fixture"})
                elif urlsplit(req.url).path == "/api/status":
                    request.fulfill(json=state["data"])
                elif urlsplit(req.url).path == "/api/predictions":
                    version = parse_qs(urlsplit(req.url).query).get("model_version", [None])[0]
                    request.fulfill(
                        json=[
                            row
                            for row in state["rows"]
                            if not version or row["model_version"] == version
                        ]
                    )
                elif req.method == "POST":
                    mutations.append({"url": urlsplit(req.url).path, "body": req.post_data_json})
                    request.fulfill(json={"status": "RECORDED"})
                else:
                    request.fulfill(status=404, json={"detail": "Unknown test route"})

            page.route("**/api/**", route)
            page.goto(url)
            expect(page.locator("#login")).to_be_visible()
            page.locator("#token").fill("wrong-test-token")
            page.locator("#login-form button").click()
            expect(page.locator("#login-error")).to_be_visible()
            page.goto(url + "/#token=" + TOKEN)
            expect(page.locator("#workspace")).to_be_visible()
            expect(page.locator("#hero-title")).to_have_text("Ready for your first prediction")
            assert "token" not in page.url
            assert page.evaluate("localStorage.length") == 0
            expect(page.locator("#open-application")).to_have_attribute(
                "href", "http://127.0.0.1:8521/"
            )
            page.screenshot(path=str(output / "overview-empty-desktop.png"), full_page=True)
            for name in ("predictions", "quality", "lifecycle", "insights", "evidence", "overview"):
                page.locator(f'[data-page="{name}"]').click()
                expect(page.locator(f"#view-{name}")).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / "overview-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1050})
            state.update(data=healthy, rows=rows)
            page.locator("#refresh").click()
            expect(page.locator("#decision")).to_have_text("Checks passed")
            page.screenshot(path=str(output / "overview-populated-desktop.png"), full_page=True)
            page.locator('[data-page="quality"]').click()
            page.locator('[data-tab="probability"]').click()
            expect(page.locator("#probability-status")).to_have_text("Evidence assessed")
            page.locator("#fraud-threshold").evaluate(
                "el => {el.value='15'; el.dispatchEvent(new Event('input'));}"
            )
            expect(page.locator("#threshold-number")).to_have_text("0.75")
            assert not mutations
            page.screenshot(path=str(output / "probability-quality-desktop.png"), full_page=True)
            page.locator('[data-tab="benchmark"]').click()
            expect(page.locator("#benchmark-results .benchmark-card")).to_have_count(2)
            evidence = page.locator("#benchmark-results details").first
            evidence.locator("summary").click()
            page.locator("#refresh").click()
            expect(evidence).to_have_attribute("open", "")
            with page.expect_download() as download:
                page.locator("#export").click()
            exported = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
            assert exported["model_version"] == VERSION and TOKEN not in json.dumps(exported)
            page.locator('[data-page="predictions"]').click()
            expect(page.locator("#predictions tr")).to_have_count(3)
            page.locator("#prediction-filter").select_option("pending")
            expect(page.locator("#predictions tr")).to_have_count(1)
            page.locator("#prediction-search").fill("no-match")
            expect(page.locator("#prediction-filter-empty")).to_be_visible()
            page.locator("#prediction-search").fill("")
            page.locator("#prediction-filter").select_option("all")
            page.locator("#model-select").select_option(OTHER)
            expect(page.locator("#prediction-empty")).to_be_visible()
            page.locator("#model-select").select_option(VERSION)
            expect(page.locator("#predictions tr")).to_have_count(3)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "predictions-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                "Predictions overflowed the mobile viewport; inspect predictions-mobile.png"
            )
            page.set_viewport_size({"width": 1440, "height": 1050})
            healthy["configuration"]["baseline_mode"] = "manual"
            page.locator("#refresh").click()
            page.locator('[data-page="evidence"]').click()
            page.locator(".evidence-disclosure summary").click()
            page.locator("#baseline").click()
            page.locator("#baseline-note").fill("Isolated representative reference fixture")
            page.locator("#baseline-form button[type=submit]").click()
            expect(page.locator("#baseline-dialog")).not_to_be_visible()
            assert mutations[-1]["body"]["model_version"] == VERSION
            healthy["configuration"]["baseline_mode"] = "auto"
            job = {
                "job_id": "ui-candidate",
                "model_version": VERSION,
                "candidate_sha256": "d" * 64,
                "status": "AWAITING_REVIEW",
                "guard_origin": "VERIFIED_BUSINESS_OUTCOMES",
                "created_at": datetime.now(UTC).isoformat(),
            }
            healthy["lifecycle"]["jobs"] = [job]
            page.locator("#refresh").click()
            page.locator('[data-page="lifecycle"]').click()
            page.get_by_role("button", name="Review approval").click()
            expect(page.locator("#review-dialog")).to_be_visible()
            page.locator("#candidate-note").fill("Isolated browser contract verification")
            page.locator("#candidate-review-submit").click()
            expect(page.locator("#review-dialog")).not_to_be_visible()
            assert mutations[-1]["body"]["candidate_sha256"] == job["candidate_sha256"]
            assert mutations[-1]["body"]["decision"] == "APPROVE"
            job["guard_origin"] = "ISOLATED_VERIFICATION_FIXTURES"
            page.locator("#refresh").click()
            expect(page.get_by_role("button", name="Review approval")).to_have_count(0)
            failed = copy.deepcopy(healthy)
            failed["reports"][0]["evaluation"]["decision"] = "FAIL"
            failed["reports"][0]["fraud_evaluation"]["status"] = "FAIL"
            state["data"] = failed
            page.locator("#refresh").click()
            page.locator('[data-page="overview"]').click()
            expect(page.locator("#decision")).to_have_text("Needs attention")
            expect(page.locator("#attention-list .attention-item")).to_have_count(1)
            failed["stale"] = True
            page.locator("#refresh").click()
            expect(page.locator("#connection-label")).to_have_text("Update overdue")
            state["data"] = healthy
            state["outage"] = True
            page.locator("#refresh").click()
            expect(page.locator("#connection-label")).to_have_text("Connection interrupted")
            expect(page.locator("#workspace")).to_be_visible()
            state["outage"] = False
            page.locator("#refresh").click()
            expect(page.locator("#connection-label")).to_have_text("Live connection")
            page.locator("#logout").click()
            expect(page.locator("#login")).to_be_visible()
            assert not errors, errors
            browser.close()
            result = {
                "passed": True,
                "fixtures_only": True,
                "browser_errors": errors,
                "scenarios": [
                    "authentication",
                    "fragment_token_removal",
                    "empty_state",
                    "six_navigation_views",
                    "mobile_overflow",
                    "model_switching",
                    "prediction_filters",
                    "probability_chart",
                    "non_mutating_threshold_preview",
                    "historical_benchmarks",
                    "disclosure_persistence",
                    "report_download",
                    "manual_baseline_binding",
                    "degraded_quality",
                    "stale_heartbeat",
                    "exact_candidate_review",
                    "fixture_promotion_blocked",
                    "outage_and_recovery",
                    "logout",
                ],
                "screenshots": [
                    str(output / name)
                    for name in (
                        "overview-empty-desktop.png",
                        "overview-mobile.png",
                        "overview-populated-desktop.png",
                        "predictions-mobile.png",
                        "probability-quality-desktop.png",
                    )
                ],
            }
            (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
