import asyncio
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.mcp_server import (
    ReadOnlyMCPService,
    create_mcp_server,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal
from agent4_deployment_recovery.runtime import (
    OPERATOR_CONFIG_ENV,
    OperatorRuntimeConfig,
    assemble_operator_mcp_server,
    assemble_read_only_runtime,
)


class Inspector:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    @property
    def cluster_identity(self):
        return self.snapshot.cluster

    def inspect_deployment(self, target):
        assert target.cluster_id == self.snapshot.cluster.cluster_id
        self.calls += 1
        return self.snapshot


class ReadAccess:
    def authorize(self, *, access_token, execution_id):
        return access_token == "trusted-read-token" and execution_id == "EXEC-mcp"


def _payload(operation):
    payload = operation.model_dump(mode="json")
    payload["authorization_token"] = "x" * 24
    return payload


def test_private_composition_root_is_operator_controlled_and_read_only(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(OPERATOR_CONFIG_ENV, raising=False)
    with pytest.raises(ValueError, match=OPERATOR_CONFIG_ENV):
        assemble_read_only_runtime()

    config_path = tmp_path / "operator.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "mode": "READ_ONLY",
                "journal_path": str((tmp_path / "journal.db").resolve()),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(OPERATOR_CONFIG_ENV, str(config_path.resolve()))
    runtime = assemble_read_only_runtime()
    assert runtime.live_execution_available is False
    assert runtime.inspector is None
    assert runtime.policy is None
    names = {
        item.name for item in asyncio.run(assemble_operator_mcp_server().list_tools())
    }
    assert "get_execution_status" not in names
    assert "get_audit_events" not in names
    with pytest.raises(TypeError):
        assemble_read_only_runtime(config={})


def test_future_live_configuration_fails_closed_and_paths_are_absolute(tmp_path):
    with pytest.raises(ValidationError, match="future live configuration"):
        OperatorRuntimeConfig(
            mode="FUTURE_LIVE", journal_path=(tmp_path / "journal.db").resolve()
        )
    with pytest.raises(ValidationError, match="journal_path"):
        OperatorRuntimeConfig(journal_path=Path("relative.db"))


def test_complete_future_configuration_is_validated_but_still_read_only(
    tmp_path, monkeypatch
):
    public_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    key_path = tmp_path / "issuer.pem"
    key_path.write_text(public_key, encoding="utf-8")
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text("not loaded while inspection is disabled", encoding="utf-8")
    config_path = tmp_path / "operator.json"
    config_path.write_text(
        json.dumps(
            {
                "mode": "FUTURE_LIVE",
                "journal_path": str((tmp_path / "journal.db").resolve()),
                "kubernetes": {
                    "mode": "KUBECONFIG",
                    "cluster_id": "cluster-dev",
                    "kubeconfig_path": str(kubeconfig_path.resolve()),
                    "context": "explicit-context",
                },
                "allowed_targets": [
                    {
                        "cluster_id": "cluster-dev",
                        "namespace": "default",
                        "workload": "fraud-api",
                        "container": "fraud-api",
                    }
                ],
                "allowed_live_environments": ["development"],
                "authorization_keys": [
                    {
                        "key_id": "AUTH-KEY-001",
                        "issuer": "trusted-authorizer",
                        "audience": "agent4-deployment-recovery",
                        "public_key_path": str(key_path.resolve()),
                    }
                ],
                "agent1_keys": [
                    {
                        "key_id": "AGENT1-KEY-001",
                        "issuer": "trusted-agent1",
                        "audience": "agent4-deployment-recovery",
                        "public_key_path": str(key_path.resolve()),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(OPERATOR_CONFIG_ENV, str(config_path.resolve()))
    runtime = assemble_read_only_runtime()
    assert runtime.live_execution_available is False
    assert runtime.inspector is None
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["authorization_keys"][0]["audience"] = "wrong-audience"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="audience"):
        assemble_read_only_runtime()


def test_default_mcp_surface_contains_only_read_only_tools():
    tools = asyncio.run(create_mcp_server().list_tools())
    names = {item.name for item in tools}
    assert names == {
        "get_capabilities",
        "validate_typed_operation",
        "preview_deployment_plan",
        "check_agent3_evidence",
        "check_agent5_evidence",
    }
    assert not names.intersection(
        {"deploy", "execute", "apply", "rollback", "configure_trust"}
    )


def test_mcp_validates_and_redacts_secrets(operation_factory):
    operation = operation_factory()
    service = ReadOnlyMCPService()
    result = service.validate_operation(_payload(operation))
    assert result["operation_id"] == operation.operation_id
    assert "authorization_token" not in json.dumps(result)
    plan = service.plan_preview(_payload(operation))
    assert plan["operation_id"] == operation.operation_id
    with pytest.raises(ValidationError):
        service.validate_operation({"operation_type": "ARBITRARY_PATCH"})


@pytest.mark.parametrize(
    "injected_field",
    ("kubeconfig_path", "cluster_context", "trusted_issuer", "mutation_permit"),
)
def test_mcp_rejects_caller_supplied_trust_and_cluster_configuration(
    operation_factory, injected_field
):
    payload = _payload(operation_factory())
    payload[injected_field] = "caller-controlled"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReadOnlyMCPService.validate_operation(payload)


def test_mcp_protocol_returns_capabilities_without_live_execution():
    result = asyncio.run(create_mcp_server().call_tool("get_capabilities", {}))
    assert result.is_error is False
    serialized = "".join(item.text for item in result.content if hasattr(item, "text"))
    assert '"live_execution_available": false' in serialized


def test_configured_mcp_inspection_does_not_accept_cluster_configuration(
    operation_factory, snapshot_factory, preflight_policy
):
    inspector = Inspector(snapshot_factory())
    service = ReadOnlyMCPService(inspector=inspector, policy=preflight_policy)
    response = service.preflight_patch_preview(_payload(operation_factory()))
    assert response["preflight"]["decision"] == "PASS"
    assert response["patch_preview"] is not None
    assert inspector.calls == 1

    tools = asyncio.run(create_mcp_server(service).list_tools())
    tool = next(item for item in tools if item.name == "preflight_patch_preview")
    assert set(tool.input_schema["properties"]) == {"payload"}
    assert "kubeconfig" not in json.dumps(tool.input_schema).lower()
    assert "context" not in json.dumps(tool.input_schema).lower()


def test_sensitive_mcp_reads_require_authentication_and_are_redacted(
    tmp_path, operation_factory
):
    operation = operation_factory()
    journal = SQLiteExecutionJournal(tmp_path / "journal.db")
    journal.create_execution(
        create_plan(operation), execution_id="EXEC-mcp", approval_id="APR-001"
    )
    service = ReadOnlyMCPService(
        journal=journal,
        execution_access=ReadAccess(),
    )
    with pytest.raises(PermissionError):
        service.execution_status("EXEC-mcp", "wrong-token")
    status = service.execution_status("EXEC-mcp", "trusted-read-token")
    assert status["execution_id"] == "EXEC-mcp"
    assert "authorization_token" not in json.dumps(status)
    assert "namespace" not in json.dumps(status)
    events = service.audit_events("EXEC-mcp", "trusted-read-token")
    assert events
    assert all("payload_json" not in event for event in events)

