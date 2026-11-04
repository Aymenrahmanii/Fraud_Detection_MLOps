"""Explicit guided refresh preserves authorization, evidence caching and fixture scope."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent6_mlops_lifecycle.assurance.interpretation import render_plan
from agent6_mlops_lifecycle.assurance.models import AdvisoryPlan
from agent6_mlops_lifecycle.monitoring.app import create_app
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings

OPERATOR = "unit-test-refresh-operator-credential-12345"


def app_for(tmp_path, *, tenant="verification", enabled=True):
    return create_app(
        MonitoringSettings(
            _env_file=None,
            tenant_id=tenant,
            token=SecretStr(OPERATOR),
            advisor_enabled=enabled,
            source_database=tmp_path / "serving.sqlite3",
            state_database=tmp_path / "state.sqlite3",
            evidence_database=tmp_path / "evidence.sqlite3",
            benchmark_database=tmp_path / "assurance.sqlite3",
        ),
        start_worker=False,
    )


def test_guided_refresh_requires_operator_and_reuses_unchanged_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "unit-test-key-not-a-real-credential")
    app = app_for(tmp_path)
    evidence = {
        "sha256:fixture": {
            "kind": "LIVE_MONITOR",
            "evidence_origin": "SCRIPTED_VERIFICATION_FIXTURES",
            "quality_decision": "UNKNOWN",
            "model_version": "v1",
            "policy_is_calibrated": False,
        }
    }
    monkeypatch.setattr(app.state.assurance, "context", lambda: evidence)
    calls = []

    async def explain(settings, context):
        calls.append(context.copy())
        return render_plan(
            context,
            AdvisoryPlan.model_validate(
                {"findings": [{"check_id": "CALIBRATE_POLICY", "evidence_ids": list(context)}]}
            ),
        )

    monkeypatch.setattr("agent6_mlops_lifecycle.assurance.service.explain", explain)
    with TestClient(app) as client:
        assert client.post("/api/presentation/advisor/refresh").status_code == 401
        headers = {"Authorization": "Bearer " + OPERATOR}
        first = client.post("/api/presentation/advisor/refresh", headers=headers)
        assert first.status_code == 200 and first.json()["advisor"]["status"] == "COMPLETED"
        again = client.post("/api/presentation/advisor/refresh", headers=headers)
        assert again.json()["advisor"]["report_ref"] == first.json()["advisor"]["report_ref"]
        assert len(calls) == 1
        evidence = {
            "sha256:changed": {
                "kind": "LIVE_MONITOR",
                "evidence_origin": "SCRIPTED_VERIFICATION_FIXTURES",
                "quality_decision": "FAIL",
                "model_version": "v1",
                "policy_is_calibrated": False,
            }
        }
        changed = client.post("/api/presentation/advisor/refresh", headers=headers)
        assert changed.status_code == 200 and changed.json()["advisor"]["status"] == "COMPLETED"
        assert len(calls) == 2


@pytest.mark.parametrize("tenant,enabled", [("local-fraud", True), ("verification", False)])
def test_explicit_refresh_cannot_bypass_production_scope_or_disabled_setting(
    tmp_path, tenant, enabled
):
    app = app_for(tmp_path, tenant=tenant, enabled=enabled)
    with TestClient(app) as client:
        response = client.post(
            "/api/presentation/advisor/refresh", headers={"Authorization": "Bearer " + OPERATOR}
        )
        assert response.status_code == 403
