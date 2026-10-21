import json
from pathlib import Path

import pytest

from agent4_deployment_recovery.adapters.compatibility import (
    TrustedArtifactProvenance,
    translate_agent3_assessment,
    translate_agent5_recommendation,
)
from agent4_deployment_recovery.core.errors import IntegrationContractError
from agent4_deployment_recovery.core.schemas import (
    KubernetesTarget,
    ScaleReplicas,
    SetContainerResources,
)

FIXTURES = Path(__file__).parent / "fixtures"
DIGEST = "sha256:" + "a" * 64
IMAGE = f"registry.example/fraud-api@{DIGEST}"


class TrustedProvenance:
    def verify(self, assessment, *, expected_image):
        del assessment
        return TrustedArtifactProvenance(
            trusted=True,
            verifier_id="trusted-ci-attestation",
            evidence_reference="oci://attestations/fraud-api/001",
            image=expected_image,
            digest=DIGEST,
            reason="digest and build provenance verified",
        )


def _agent3(**updates):
    payload = json.loads((FIXTURES / "agent3_release_assessment.json").read_text())
    payload["artifact"] = {"image": "fraud-api", "digest": DIGEST}
    payload.update(updates)
    return json.dumps(payload)


def test_agent3_block_and_missing_digest_fail_closed():
    with pytest.raises(IntegrationContractError, match="BLOCK"):
        translate_agent3_assessment(
            _agent3(decision="BLOCK", blocking_reasons=["security gate"]),
            expected_image=IMAGE,
            provenance_verifier=TrustedProvenance(),
        )
    raw = json.loads(_agent3())
    raw["artifact"]["digest"] = ""
    with pytest.raises(IntegrationContractError, match="missing"):
        translate_agent3_assessment(
            json.dumps(raw),
            expected_image=IMAGE,
            provenance_verifier=TrustedProvenance(),
        )


def test_agent3_warning_remains_evidence_and_requires_trusted_ci():
    metadata = translate_agent3_assessment(
        _agent3(decision="ALLOW_WITH_WARNING", warnings=["coverage warning"]),
        expected_image=IMAGE,
        provenance_verifier=TrustedProvenance(),
    )
    assert metadata.decision == "ALLOW_WITH_WARNING"
    assert metadata.requires_independent_authorization is True
    with pytest.raises(IntegrationContractError, match="not configured"):
        translate_agent3_assessment(_agent3(), expected_image=IMAGE)


class Resolver:
    def __init__(self, count=1):
        self.count = count

    def resolve(self, *, environment, service_id, namespace):
        del environment, service_id
        return tuple(
            KubernetesTarget(
                cluster_id="cluster-dev",
                namespace=namespace,
                name=f"fraud-api-{index}",
                container="fraud-api",
                uid=f"uid-fraud-api-{index}",
                expected_resource_version="12345",
            )
            for index in range(self.count)
        )


def _agent5_scale():
    payload = json.loads((FIXTURES / "agent5_resource_recommendation.json").read_text())
    payload["payload"]["recommendation"].update(
        {"action": "SCALE_REPLICAS", "from": 2, "to": 3}
    )
    return json.dumps(payload)


def test_agent5_translation_preserves_source_and_never_authorizes():
    proposal = translate_agent5_recommendation(_agent5_scale(), resolver=Resolver())
    assert proposal.recommendation_id == "RES-043BF15A8809"
    assert proposal.correlation_id == "INC-351DEE35057B"
    assert isinstance(proposal.operation, ScaleReplicas)
    assert proposal.constitutes_authorization is False
    assert proposal.requires_independent_authorization is True


def test_agent5_ambiguous_target_and_no_change_behavior():
    with pytest.raises(IntegrationContractError, match="ambiguous"):
        translate_agent5_recommendation(_agent5_scale(), resolver=Resolver(2))
    no_change = (FIXTURES / "agent5_resource_recommendation.json").read_text()
    proposal = translate_agent5_recommendation(no_change)
    assert proposal.operation is None
    assert proposal.target is None


def test_agent5_memory_increase_resolves_exact_target_and_remains_advisory():
    payload = json.loads(
        (FIXTURES / "agent5_resource_recommendation.json").read_text()
    )
    payload["payload"]["recommendation"].update(
        {
            "action": "INCREASE_MEMORY",
            "memory_limit_from_mib": 256.0,
            "memory_limit_to_mib": 384.0,
        }
    )
    proposal = translate_agent5_recommendation(
        json.dumps(payload), resolver=Resolver()
    )
    assert isinstance(proposal.operation, SetContainerResources)
    assert proposal.operation.memory_limit_mib == 384
    assert proposal.target is not None
    assert proposal.target.uid == "uid-fraud-api-0"
    assert proposal.constitutes_authorization is False
