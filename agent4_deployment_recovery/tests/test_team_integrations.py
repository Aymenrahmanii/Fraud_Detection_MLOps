import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent4_deployment_recovery.adapters.agent1_compatibility import (
    read_agent1_recovery_event,
)
from agent4_deployment_recovery.adapters.agent2_compatibility import (
    translate_agent2_diagnosis,
)
from agent4_deployment_recovery.adapters.agent6_compatibility import (
    Agent6ResolvedImage,
    Agent6ResolvedRollback,
    translate_agent6_deployment_request,
    translate_agent6_rollback_request,
)
from agent4_deployment_recovery.core.errors import IntegrationContractError
from agent4_deployment_recovery.core.schemas import (
    DeployImageDigest,
    KubernetesTarget,
    RollbackExecution,
    ScaleReplicas,
)


def _synthetic_identifier(prefix: str) -> str:
    return prefix + "-" + ("0" * 24)


class WorkloadResolver:
    def __init__(self, count: int = 1):
        self.count = count

    def resolve(self, **kwargs):
        namespace = kwargs.get("namespace", "default")
        return tuple(
            KubernetesTarget(
                cluster_id="cluster-dev",
                namespace=namespace,
                name="fraud-api",
                container="fraud-api",
                uid=f"uid-{index}",
                expected_resource_version="123",
            )
            for index in range(self.count)
        )


def test_current_agent1_event_is_evidence_not_trusted_verification():
    payload = {
        "message_id": "MSG-001",
        "schema_version": "1.0",
        "correlation_id": "CORR-001",
        "causation_id": "EVT-001",
        "trace_id": "TRACE-001",
        "source": "monitoring-agent",
        "target": "orchestrator",
        "topic": "monitoring.recovery.verified",
        "message_type": "RECOVERY_VERIFIED",
        "priority": "P1",
        "severity": "HIGH",
        "timestamp": datetime.now(UTC).isoformat(),
        "deadline": None,
        "environment": "staging",
        "service": {
            "service_id": "fraud-api",
            "namespace": "default",
            "version": "v1",
        },
        "evidence_refs": ["prometheus://window/1"],
        "confidence": 0.9,
        "requires_ack": True,
        "idempotency_key": _synthetic_identifier("IDEMP"),
        "payload": {
            "verification_id": "VER-001",
            "action_id": "ACTION-001",
            "window": "5m",
            "status": "RECOVERED",
            "checks": {"error_rate": "PASS"},
        },
    }
    evidence = read_agent1_recovery_event(json.dumps(payload))
    assert evidence.status == "RECOVERED"
    assert evidence.constitutes_trusted_verification is False
    assert any("not signed" in item for item in evidence.limitations)


def _agent2_payload(action: str, parameters: dict) -> str:
    return json.dumps(
        {
            "diagnosis_id": "DIAG-001",
            "hypotheses": [
                {
                    "rank": 1,
                    "code": "TRAFFIC_SPIKE",
                    "probability": 0.8,
                    "evidence_refs": ["EVD-001"],
                }
            ],
            "primary_root_cause": "TRAFFIC_SPIKE",
            "recommended_actions": [
                {
                    "action_id": "ACTION-001",
                    "action_version": 1,
                    "incident_id": "INC-001",
                    "action": action,
                    "environment": "staging",
                    "target": {"service": "fraud-api", "namespace": "default"},
                    "parameters": parameters,
                    "preconditions": [],
                    "verification_profile": "fraud-api-slo-default",
                }
            ],
            "rationale_summary": "bounded test diagnosis",
            "limitations": [],
            "requires_additional_evidence": False,
        }
    )


def test_agent2_supported_action_becomes_non_authorizing_typed_proposal():
    proposal = translate_agent2_diagnosis(
        _agent2_payload(
            "SCALE_REPLICAS", {"from_replicas": 2, "to_replicas": 3}
        ),
        action_id="ACTION-001",
        resolver=WorkloadResolver(),
    )
    assert isinstance(proposal.operation, ScaleReplicas)
    assert proposal.constitutes_authorization is False
    assert proposal.requires_independent_authorization is True


def test_agent2_unsupported_or_underspecified_actions_fail_closed():
    with pytest.raises(IntegrationContractError, match="unsupported"):
        translate_agent2_diagnosis(
            _agent2_payload("ROLLING_RESTART", {}),
            action_id="ACTION-001",
            resolver=WorkloadResolver(),
        )
    with pytest.raises(ValidationError):
        translate_agent2_diagnosis(
            _agent2_payload("SCALE_REPLICAS", {"service": "fraud-api"}),
            action_id="ACTION-001",
            resolver=WorkloadResolver(),
        )


class ArtifactResolver:
    def resolve(self, request):
        return Agent6ResolvedImage(
            trusted=True,
            resolver_id="trusted-model-packager",
            evidence_reference="oci://attestation/agent6-001",
            candidate_sha256=request.candidate_sha256,
            image="registry.example/fraud-api@sha256:" + ("a" * 64),
            model_version="artifact-" + request.candidate_sha256,
        )


class Agent6WorkloadResolver:
    def __init__(self, count: int = 1):
        self.count = count

    def resolve(self, *, scope):
        del scope
        return WorkloadResolver(self.count).resolve(namespace="default")


def _agent6_deployment_payload(*, expires_at: datetime) -> str:
    candidate = "b" * 64
    return json.dumps(
        {
            "schema_version": "1",
            "kind": "MODEL_DEPLOYMENT_REQUEST",
            "request_id": "JOB-001",
            "candidate_sha256": candidate,
            "candidate_path": "artifacts/agent6/candidates/candidate.pkl",
            "evaluation_ref": "EVAL-001",
            "previous_model_version": "champion-v1",
            "review_ref": "REVIEW-001",
            "scope": {
                "tenant_id": "local-fraud",
                "project_id": "fraud-detection-serving",
                "model_id": "fraud-api-pipeline",
            },
            "requires_post_deploy_verification": True,
            "rollback_condition": "independently measured regression",
            "artifact_location": {
                "kind": "shared-agent6-volume",
                "relative_path": "candidates/candidate.pkl",
            },
            "destination": "deployment-agent",
            "expires_at": expires_at.isoformat(),
            "preconditions": {
                "active_model_version": "champion-v1",
                "candidate_sha256": candidate,
            },
        }
    )


def test_agent6_candidate_requires_attested_image_and_exact_workload():
    proposal = translate_agent6_deployment_request(
        _agent6_deployment_payload(expires_at=datetime.now(UTC) + timedelta(hours=1)),
        artifact_resolver=ArtifactResolver(),
        workload_resolver=Agent6WorkloadResolver(),
    )
    assert isinstance(proposal.operation, DeployImageDigest)
    assert proposal.operation.image.startswith("registry.example/")
    assert proposal.constitutes_authorization is False

    with pytest.raises(IntegrationContractError, match="expired"):
        translate_agent6_deployment_request(
            _agent6_deployment_payload(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            ),
            artifact_resolver=ArtifactResolver(),
            workload_resolver=Agent6WorkloadResolver(),
        )


class RollbackResolver:
    def resolve(self, request):
        return Agent6ResolvedRollback(
            trusted=True,
            resolver_id="agent6-execution-index",
            evidence_reference=request.evidence_ref,
            current_model_version=request.current_model_version,
            restore_model_version=request.restore_model_version,
            source_execution_id="EXEC-001",
            source_plan_hash="c" * 64,
            target=WorkloadResolver().resolve(namespace="default")[0],
        )


def test_agent6_rollback_maps_only_through_trusted_execution_binding():
    raw = json.dumps(
        {
            "schema_version": "1",
            "kind": "MODEL_ROLLBACK_REQUEST",
            "request_id": "RB-JOB-001",
            "original_job_id": "JOB-001",
            "current_model_version": "artifact-" + ("b" * 64),
            "restore_model_version": "champion-v1",
            "evidence_ref": "REPORT-001",
            "destination": "deployment-agent",
            "scope": {
                "tenant_id": "local-fraud",
                "project_id": "fraud-detection-serving",
                "model_id": "fraud-api-pipeline",
            },
        }
    )
    proposal = translate_agent6_rollback_request(raw, resolver=RollbackResolver())
    assert isinstance(proposal.operation, RollbackExecution)
    assert proposal.constitutes_authorization is False
    with pytest.raises(IntegrationContractError, match="not configured"):
        translate_agent6_rollback_request(raw, resolver=None)
