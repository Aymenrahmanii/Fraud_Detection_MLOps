import json
from datetime import UTC, datetime, timedelta
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
    NoOp,
    ScaleReplicas,
    SetContainerResources,
)

FIXTURES = Path(__file__).parent / "fixtures"
DIGEST = "sha256:" + "a" * 64
IMAGE = f"registry.example/fraud-api@{DIGEST}"
SOURCE_REVISION = "56c48f9667017db3e401227a04dafb0598df2f9b"


class TrustedProvenance:
    def verify(self, assessment, *, expected_image):
        return TrustedArtifactProvenance(
            trusted=True,
            verifier_id="trusted-ci-attestation",
            evidence_reference="oci://attestations/fraud-api/001",
            image=expected_image,
            digest=DIGEST,
            source_revision=assessment.commit,
            issued_at=datetime.now(UTC) - timedelta(minutes=1),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            reason="digest and build provenance verified",
        )


class ExpiredProvenance:
    def verify(self, assessment, *, expected_image):
        now = datetime.now(UTC)
        return TrustedArtifactProvenance(
            trusted=True,
            verifier_id="trusted-ci-attestation",
            evidence_reference="oci://attestations/fraud-api/expired",
            image=expected_image,
            digest=DIGEST,
            source_revision=assessment.commit,
            issued_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
            reason="expired test provenance",
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
            expected_source_revision=SOURCE_REVISION,
            provenance_verifier=TrustedProvenance(),
        )
    raw = json.loads(_agent3())
    raw["artifact"]["digest"] = ""
    with pytest.raises(IntegrationContractError, match="missing"):
        translate_agent3_assessment(
            json.dumps(raw),
            expected_image=IMAGE,
            expected_source_revision=SOURCE_REVISION,
            provenance_verifier=TrustedProvenance(),
        )


def test_agent3_warning_remains_evidence_and_requires_trusted_ci():
    metadata = translate_agent3_assessment(
        _agent3(decision="ALLOW_WITH_WARNING", warnings=["coverage warning"]),
        expected_image=IMAGE,
        expected_source_revision=SOURCE_REVISION,
        provenance_verifier=TrustedProvenance(),
    )
    assert metadata.decision == "ALLOW_WITH_WARNING"
    assert metadata.requires_independent_authorization is True
    with pytest.raises(IntegrationContractError, match="not configured"):
        translate_agent3_assessment(
            _agent3(), expected_image=IMAGE, expected_source_revision=SOURCE_REVISION
        )


def test_agent3_source_revision_freshness_and_current_findings_schema():
    payload = json.loads(_agent3())
    payload["security"]["findings"] = [
        {
            "scanner": "gitleaks",
            "rule": "generic-api-key",
            "message": "redacted fixture finding",
            "file": "tests/fixture.py",
            "line": 7,
            "severity": "HIGH",
        }
    ]
    metadata = translate_agent3_assessment(
        json.dumps(payload),
        expected_image=IMAGE,
        expected_source_revision=SOURCE_REVISION,
        provenance_verifier=TrustedProvenance(),
    )
    assert metadata.commit == SOURCE_REVISION

    with pytest.raises(IntegrationContractError, match="source revision"):
        translate_agent3_assessment(
            _agent3(),
            expected_image=IMAGE,
            expected_source_revision="different-commit",
            provenance_verifier=TrustedProvenance(),
        )

    with pytest.raises(IntegrationContractError, match="expired"):
        translate_agent3_assessment(
            _agent3(),
            expected_image=IMAGE,
            expected_source_revision=SOURCE_REVISION,
            provenance_verifier=ExpiredProvenance(),
        )


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
    proposal = translate_agent5_recommendation(no_change, resolver=Resolver())
    assert isinstance(proposal.operation, NoOp)
    assert proposal.target is not None
    with pytest.raises(IntegrationContractError, match="resolution"):
        translate_agent5_recommendation(no_change)


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
