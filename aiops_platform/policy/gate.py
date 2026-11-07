"""Policy & Risk Gate (v5 sections 7.1 and 17).

A pure, deterministic function: the same inputs always give the same decision,
and every decision lists the reasons and HITL triggers that produced it, so the
audit trail explains it without any hidden reasoning.

    DecisionScore = 0.40*RCA_confidence + 0.25*evidence_strength
                  + 0.20*historical_success + 0.15*agent_agreement
    RiskScore     = f(environment_criticality, blast_radius, reversibility,
                      data_impact, uncertainty)

    forbidden action / missing precondition -> BLOCK
    LOW risk and no HITL trigger and score >= auto_threshold -> AUTO
    otherwise -> APPROVAL_REQUIRED
"""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import JsonValue

from aiops_platform.contracts.enums import PolicyDecision

DEFAULT_POLICY = Path(__file__).with_name("policy.yaml")


@dataclass(frozen=True)
class PolicyInput:
    action: str
    environment: str
    parameters: dict[str, JsonValue]
    rca_confidence: float
    evidence_strength: float
    agent_agreement: float = 1.0
    historical_success: float | None = None
    # Preconditions the Orchestrator checked (all must hold).
    evidence_complete: bool = True
    context_fresh: bool = True
    release_gate: str | None = None  # Agent 3 decision when the action deploys an artifact
    security_exception: bool = False
    data_or_schema_operation: bool = False


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    score: float
    blast_radius: float
    reversible: bool
    factors: dict[str, float]


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    decision_score: float
    risk: RiskAssessment
    reasons: tuple[str, ...]
    hitl_triggers: tuple[str, ...] = ()
    policy_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "decision": self.decision.value,
            "decision_score": self.decision_score,
            "risk": {
                "level": self.risk.level,
                "score": self.risk.score,
                "blast_radius": self.risk.blast_radius,
                "reversible": self.risk.reversible,
                "factors": dict(self.risk.factors),
            },
            "reasons": list(self.reasons),
            "hitl_triggers": list(self.hitl_triggers),
            "policy_version": self.policy_version,
        }


@lru_cache(maxsize=8)
def load_policy(path: str | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_POLICY, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class PolicyGate:
    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        self._p = policy or load_policy()

    @property
    def version(self) -> str:
        return str(self._p.get("version", "unversioned"))

    def decision_score(self, x: PolicyInput) -> float:
        w = self._p["decision_weights"]
        historical = (
            x.historical_success
            if x.historical_success is not None
            else self._p["thresholds"]["default_historical_success"]
        )
        score = (
            w["rca_confidence"] * _clip(x.rca_confidence)
            + w["evidence_strength"] * _clip(x.evidence_strength)
            + w["historical_success"] * _clip(historical)
            + w["agent_agreement"] * _clip(x.agent_agreement)
        )
        return round(score, 4)

    def risk(self, x: PolicyInput, decision_score: float) -> RiskAssessment:
        profile = self._p["actions"].get(
            x.action, {"blast_radius": 1.0, "reversible": False, "data_impact": 1.0}
        )
        w = self._p["risk_weights"]
        factors = {
            "environment_criticality": _clip(self._p["environment_criticality"].get(x.environment, 1.0)),
            "blast_radius": _clip(profile["blast_radius"]),
            "irreversibility": 0.0 if profile["reversible"] else 1.0,
            "data_impact": _clip(1.0 if x.data_or_schema_operation else profile["data_impact"]),
            "uncertainty": _clip(1.0 - decision_score),
        }
        score = round(sum(w[name] * value for name, value in factors.items()), 4)
        levels = self._p["thresholds"]["risk_levels"]
        level = next((name for name in ("LOW", "MEDIUM", "HIGH") if score < levels[name]), "CRITICAL")
        return RiskAssessment(
            level=level,
            score=score,
            blast_radius=factors["blast_radius"],
            reversible=bool(profile["reversible"]),
            factors=factors,
        )

    def _out_of_bounds(self, x: PolicyInput) -> str | None:
        if x.action != "SCALE_REPLICAS":
            return None
        bounds = self._p["bounds"]
        target = x.parameters.get("replicas", x.parameters.get("to"))
        current = x.parameters.get("from")
        if not isinstance(target, int):
            return "scale target replica count is missing"
        if not bounds["min_replicas"] <= target <= bounds["max_replicas"]:
            return f"replicas={target} is outside [{bounds['min_replicas']}, {bounds['max_replicas']}]"
        if isinstance(current, int) and abs(target - current) > bounds["max_scale_step"]:
            return f"scale step {current}->{target} exceeds {bounds['max_scale_step']}"
        return None

    def evaluate(self, x: PolicyInput) -> PolicyResult:
        score = self.decision_score(x)
        risk = self.risk(x, score)
        thresholds = self._p["thresholds"]
        block: list[str] = []
        triggers: list[str] = []

        if {"environment": x.environment, "action": x.action} in self._p.get("forbidden", []):
            block.append(f"{x.action} is forbidden in {x.environment}")
        if x.action not in self._p["executable_actions"]:
            block.append(f"{x.action} has no authorized executor (Agent 4 does not support it)")
        if not x.context_fresh:
            block.append("task context is stale; the action must be re-planned")
        if x.release_gate == "BLOCK":
            block.append("Agent 3 release gate is BLOCK")
        if not x.evidence_complete:
            block.append("mandatory evidence is missing")
        bound_violation = self._out_of_bounds(x)

        pre_authorized = {"environment": x.environment, "action": x.action} in self._p.get(
            "pre_authorized", []
        )
        if x.environment == "production" and not pre_authorized:
            triggers.append("production side effect without pre-authorization")
        if not risk.reversible:
            triggers.append("destructive or difficult-to-reverse operation")
        if x.data_or_schema_operation:
            triggers.append("database/schema or persistent-data operation")
        if x.security_exception or x.release_gate == "ALLOW_WITH_WARNING":
            triggers.append("security gate warning or exception")
        if x.rca_confidence < thresholds["min_confidence"]:
            triggers.append(
                f"decision confidence {x.rca_confidence:.2f} below {thresholds['min_confidence']:.2f}"
            )
        if x.agent_agreement < 1 - thresholds["max_disagreement"]:
            triggers.append(f"high specialist disagreement (agreement {x.agent_agreement:.2f})")
        if bound_violation:
            triggers.append(f"outside bounded autonomous range: {bound_violation}")

        if block:
            decision, reasons = PolicyDecision.BLOCK, block
        elif triggers or risk.level != "LOW" or score < thresholds["auto_decision_score"]:
            decision = PolicyDecision.APPROVAL_REQUIRED
            reasons = list(triggers)
            if risk.level != "LOW":
                reasons.append(f"risk level {risk.level} (score {risk.score:.2f})")
            if score < thresholds["auto_decision_score"]:
                reasons.append(
                    f"decision score {score:.2f} below auto threshold {thresholds['auto_decision_score']:.2f}"
                )
        else:
            decision = PolicyDecision.AUTO
            reasons = [f"LOW risk, decision score {score:.2f}, within bounded autonomy"]
        return PolicyResult(
            decision=decision,
            decision_score=score,
            risk=risk,
            reasons=tuple(reasons),
            hitl_triggers=tuple(triggers),
            policy_version=self.version,
        )
