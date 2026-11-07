"""Policy & Risk Gate (v5 sections 7.1 and 17)."""

from aiops_platform.contracts.enums import PolicyDecision
from aiops_platform.policy import PolicyGate, PolicyInput

gate = PolicyGate()


def x(**overrides):
    base = dict(
        action="SCALE_REPLICAS",
        environment="staging",
        parameters={"from": 3, "to": 6},
        rca_confidence=0.95,
        evidence_strength=0.95,
        agent_agreement=1.0,
        historical_success=0.9,
    )
    return PolicyInput(**{**base, **overrides})


def test_decision_score_uses_published_weights():
    assert gate.decision_score(x(rca_confidence=1, evidence_strength=1, historical_success=1, agent_agreement=1)) == 1.0
    assert (
        gate.decision_score(x(rca_confidence=0.9, evidence_strength=0.8, historical_success=0.5, agent_agreement=1))
        == 0.81
    )


def test_bounded_scale_in_staging_is_auto():
    result = gate.evaluate(x())
    assert result.decision is PolicyDecision.AUTO
    assert result.risk.level == "LOW"
    assert result.hitl_triggers == ()


def test_production_rollback_requires_approval():
    result = gate.evaluate(
        x(action="ROLLBACK", environment="production", parameters={"to_version": "v2.6"}, rca_confidence=0.86)
    )
    assert result.decision is PolicyDecision.APPROVAL_REQUIRED
    assert "production side effect without pre-authorization" in result.hitl_triggers


def test_low_confidence_and_disagreement_force_hitl():
    result = gate.evaluate(x(rca_confidence=0.4, agent_agreement=0.3))
    assert result.decision is PolicyDecision.APPROVAL_REQUIRED
    assert any("confidence" in t for t in result.hitl_triggers)
    assert any("disagreement" in t for t in result.hitl_triggers)


def test_scale_outside_bounds_needs_a_human():
    result = gate.evaluate(x(parameters={"from": 3, "to": 12}))
    assert result.decision is PolicyDecision.APPROVAL_REQUIRED
    assert any("bounded autonomous range" in t for t in result.hitl_triggers)


def test_blocking_conditions():
    assert gate.evaluate(x(action="CANARY")).decision is PolicyDecision.BLOCK  # no executor
    assert gate.evaluate(x(context_fresh=False)).decision is PolicyDecision.BLOCK
    assert gate.evaluate(x(action="DEPLOY", parameters={}, release_gate="BLOCK")).decision is PolicyDecision.BLOCK
    assert gate.evaluate(x(evidence_complete=False)).decision is PolicyDecision.BLOCK
    assert gate.evaluate(x(action="BLUE_GREEN_SWITCH", environment="production")).decision is PolicyDecision.BLOCK


def test_result_is_auditable_json():
    data = gate.evaluate(x()).as_json()
    assert data["decision"] == "AUTO"
    assert set(data["risk"]["factors"]) == {
        "environment_criticality",
        "blast_radius",
        "irreversibility",
        "data_impact",
        "uncertainty",
    }
    assert data["policy_version"] == "platform-policy-1.0"
