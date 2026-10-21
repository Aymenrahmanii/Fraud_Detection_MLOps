from pathlib import Path

import pytest
from pydantic import ValidationError

from agent4_deployment_recovery.adapters.compatibility import (
    read_agent3_assessment,
    read_agent5_recommendation,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_agent3_current_contract_is_read_as_non_authorizing_evidence():
    evidence = read_agent3_assessment(
        (FIXTURES / "agent3_release_assessment.json").read_text(encoding="utf-8")
    )
    assert evidence.decision == "ALLOW"
    assert evidence.constitutes_authorization is False
    assert evidence.digest == ""


def test_agent5_current_contract_is_read_as_non_authorizing_evidence():
    evidence = read_agent5_recommendation(
        (FIXTURES / "agent5_resource_recommendation.json").read_text(encoding="utf-8")
    )
    assert evidence.action == "NO_CHANGE"
    assert evidence.constitutes_authorization is False
    assert evidence.service_id == "fraud-api"


def test_unknown_agent_schema_versions_fail_closed():
    agent3 = (FIXTURES / "agent3_release_assessment.json").read_text(encoding="utf-8")
    agent5 = (FIXTURES / "agent5_resource_recommendation.json").read_text(
        encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        read_agent3_assessment(
            agent3.replace('"schema_version": "1.0"', '"schema_version": "2.0"')
        )
    with pytest.raises(ValidationError):
        read_agent5_recommendation(
            agent5.replace('"schema_version": "1.0"', '"schema_version": "2.0"')
        )


def test_unknown_agent_fields_fail_closed():
    raw = (FIXTURES / "agent3_release_assessment.json").read_text(encoding="utf-8")
    altered = raw.replace(
        '"rationale": null', '"rationale": null, "authorization": true'
    )
    with pytest.raises(ValidationError):
        read_agent3_assessment(altered)
