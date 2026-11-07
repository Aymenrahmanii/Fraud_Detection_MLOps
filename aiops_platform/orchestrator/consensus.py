"""Consensus and conflict detection (v5 section 17).

Specialist outputs are not averaged. Each typed recommendation becomes a
candidate action with a score; agreement measures whether the specialists'
recommendations are compatible; the Policy & Risk Gate makes the final call.
"""

from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue

# Root causes for which more capacity only buys time (v5 scenario "scale-up is a
# temporary alternative to rollback").
# Codes are Agent 2's (agent2_diagnosis/rules.py).
CAPACITY_DOES_NOT_FIX = {
    "MEMORY_LEAK_POST_DEPLOY",
    "CONFIG_ERROR_CRASHLOOP",
    "CRASH_LOOP_UNEXPLAINED",
    "CONNECTION_POOL_EXHAUSTION",
    "DEPENDENCY_UNAVAILABLE",
}
CONTAINMENT = {"SCALE_REPLICAS", "INCREASE_MEMORY"}


@dataclass
class Candidate:
    action_id: str
    action_version: int
    type: str
    source_agent: str
    score: float
    parameters: dict[str, JsonValue]
    rationale: str
    intent: dict[str, JsonValue] = field(default_factory=dict)

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "action_id": self.action_id,
            "action_version": self.action_version,
            "type": self.type,
            "source_agent": self.source_agent,
            "score": round(self.score, 4),
            "parameters": self.parameters,
            "rationale": self.rationale,
            "intent": self.intent,
        }


@dataclass
class Consensus:
    candidates: list[Candidate]
    rca_confidence: float
    evidence_strength: float
    agreement: float
    primary_root_cause: str | None
    requires_additional_evidence: bool
    conflicts: list[str]

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "candidates": [c.as_json() for c in self.candidates],
            "rca_confidence": round(self.rca_confidence, 4),
            "evidence_strength": round(self.evidence_strength, 4),
            "agreement": round(self.agreement, 4),
            "primary_root_cause": self.primary_root_cause,
            "requires_additional_evidence": self.requires_additional_evidence,
            "conflicts": self.conflicts,
        }


def build_consensus(
    diagnosis: dict[str, Any] | None,
    resource: dict[str, Any] | None,
    *,
    monitoring_confidence: float | None = None,
) -> Consensus:
    candidates: list[Candidate] = []
    conflicts: list[str] = []
    rca_confidence = 0.0
    root = None
    more_evidence = False
    evidence_strength = monitoring_confidence or 0.5

    if diagnosis:
        hypotheses = diagnosis.get("hypotheses") or []
        rca_confidence = float(hypotheses[0]["probability"]) if hypotheses else 0.0
        root = diagnosis.get("primary_root_cause")
        more_evidence = bool(diagnosis.get("requires_additional_evidence"))
        if isinstance(diagnosis.get("evidence_strength"), int | float):
            evidence_strength = float(diagnosis["evidence_strength"])
        for index, intent in enumerate(diagnosis.get("recommended_actions") or []):
            # The first recommendation carries the leading hypothesis' probability;
            # later ones are fallbacks.
            score = rca_confidence * (0.5**index)
            candidates.append(
                Candidate(
                    action_id=intent["action_id"],
                    action_version=int(intent.get("action_version", 1)),
                    type=intent["action"],
                    source_agent="diagnosis-agent",
                    score=score,
                    parameters=dict(intent.get("parameters") or {}),
                    rationale=f"RCA: {root} (p={rca_confidence:.2f})",
                    intent=intent,
                )
            )

    agreement = 0.8 if not (diagnosis and resource) else 1.0
    if resource and resource.get("candidate_action"):
        intent = resource["candidate_action"]
        confidence = float(resource.get("confidence") or 0.5)
        temporary = root in CAPACITY_DOES_NOT_FIX
        score = confidence * (0.5 if temporary else 1.0)
        candidates.append(
            Candidate(
                action_id=intent["action_id"],
                action_version=int(intent.get("action_version", 1)),
                type=intent["action"],
                source_agent="resource-optimization-agent",
                score=score,
                parameters=dict(intent.get("parameters") or {}),
                rationale=(
                    "capacity change only buys time for " + str(root)
                    if temporary
                    else "resource policy threshold exceeded"
                ),
                intent=intent,
            )
        )
        rca_types = {c.type for c in candidates if c.source_agent == "diagnosis-agent"}
        if rca_types and intent["action"] not in rca_types:
            if intent["action"] in CONTAINMENT:
                agreement = 0.75  # compatible: containment next to a fix
                conflicts.append(f"resource agent proposes {intent['action']} while RCA proposes {sorted(rca_types)}")
            else:
                agreement = 0.3
                conflicts.append(f"incompatible: {intent['action']} vs {sorted(rca_types)}")
    if more_evidence:
        agreement = min(agreement, 0.45)
        conflicts.append("diagnosis requires additional evidence")
    candidates.sort(key=lambda c: c.score, reverse=True)
    return Consensus(
        candidates=candidates,
        rca_confidence=rca_confidence,
        evidence_strength=evidence_strength,
        agreement=agreement,
        primary_root_cause=root,
        requires_additional_evidence=more_evidence,
        conflicts=conflicts,
    )
