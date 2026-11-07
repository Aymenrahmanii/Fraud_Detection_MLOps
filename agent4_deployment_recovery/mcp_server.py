"""Read-only MCP surface for Agent 4.

The default server has no cluster adapter, journal, credential verifier, or
mutation path. Sensitive status/audit tools are registered only by a private
composition root that injects an authenticated read-access verifier.
"""

import json
from typing import Protocol, runtime_checkable

from mcp.server.mcpserver import MCPServer

from agent4_deployment_recovery.adapters.compatibility import (
    read_agent3_assessment,
    read_agent5_recommendation,
)
from agent4_deployment_recovery.adapters.kubernetes import KubernetesInspectionAdapter
from agent4_deployment_recovery.core.canonical import canonical_sha256
from agent4_deployment_recovery.core.kubernetes_models import (
    PreflightDecision,
    PreflightPolicy,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.schemas import AuthorizedOperation, OperationType
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


@runtime_checkable
class ExecutionReadAccessVerifier(Protocol):
    def authorize(self, *, access_token: str, execution_id: str) -> bool:
        """Authenticate access to one execution without trusting caller identity text."""


class ReadOnlyMCPService:
    def __init__(
        self,
        *,
        inspector: KubernetesInspectionAdapter | None = None,
        policy: PreflightPolicy | None = None,
        journal: SQLiteExecutionJournal | None = None,
        execution_access: ExecutionReadAccessVerifier | None = None,
    ):
        if (inspector is None) != (policy is None):
            raise ValueError("inspection adapter and policy must be configured together")
        if (journal is None) != (execution_access is None):
            raise ValueError(
                "sensitive execution tools require both journal and access verifier"
            )
        self.inspector = inspector
        self.policy = policy
        self.journal = journal
        self.execution_access = execution_access

    @staticmethod
    def capabilities() -> dict:
        return {
            "schema_version": "1.0",
            "service": "agent4-deployment-recovery",
            "mode": "READ_ONLY",
            "supported_operations": [item.value for item in OperationType],
            "tools_never_exposed": [
                "deploy",
                "execute",
                "apply",
                "rollback",
                "arbitrary_shell",
                "arbitrary_patch",
                "configure_trust",
            ],
            "live_execution_available": False,
            "traffic_routing": "UNKNOWN_UNSUPPORTED",
        }

    @staticmethod
    def validate_operation(payload: dict) -> dict:
        operation = AuthorizedOperation.model_validate_json(json.dumps(payload))
        operation.ensure_fresh()
        return operation.model_dump(mode="json", exclude={"authorization_token"})

    @staticmethod
    def plan_preview(payload: dict) -> dict:
        operation = AuthorizedOperation.model_validate_json(json.dumps(payload))
        return create_plan(operation).model_dump(mode="json")

    def preflight_patch_preview(self, payload: dict) -> dict:
        if self.inspector is None or self.policy is None:
            raise PermissionError("operator-configured inspection is unavailable")
        operation = AuthorizedOperation.model_validate_json(json.dumps(payload))
        plan = create_plan(operation)
        snapshot = self.inspector.inspect_deployment(operation.target)
        result = run_preflight(
            operation,
            snapshot,
            self.policy,
            requested_strategy=plan.strategy,
        )
        response = {
            "preflight": result.model_dump(mode="json"),
            "snapshot_hash": canonical_sha256(snapshot),
            "patch_preview": None,
        }
        if result.decision == PreflightDecision.PASS:
            response["patch_preview"] = build_typed_patch(
                operation, snapshot, result
            ).model_dump(mode="json")
        return response

    @staticmethod
    def check_agent3_evidence(payload_json: str) -> dict:
        return read_agent3_assessment(payload_json).model_dump(mode="json")

    @staticmethod
    def check_agent5_evidence(payload_json: str) -> dict:
        return read_agent5_recommendation(payload_json).model_dump(mode="json")

    def _authorize_read(self, access_token: str, execution_id: str) -> None:
        if (
            self.execution_access is None
            or self.journal is None
            or not self.execution_access.authorize(
                access_token=access_token, execution_id=execution_id
            )
        ):
            raise PermissionError("authenticated execution read access denied")

    def execution_status(self, execution_id: str, access_token: str) -> dict:
        self._authorize_read(access_token, execution_id)
        if self.journal is None:
            raise PermissionError("execution journal is unavailable")
        record = self.journal.get_execution(execution_id)
        return {
            "schema_version": record.schema_version,
            "execution_id": record.execution_id,
            "operation_id": record.operation_id,
            "correlation_id": record.correlation_id,
            "trace_id": record.trace_id,
            "operation_type": record.operation_type.value,
            "state": record.state.value,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "version": record.version,
            "target_identity_hash": canonical_sha256(record.target),
            "verification_verdict": (
                record.verification.verdict.value if record.verification else None
            ),
        }

    def audit_events(self, execution_id: str, access_token: str) -> list[dict]:
        self._authorize_read(access_token, execution_id)
        if self.journal is None:
            raise PermissionError("execution journal is unavailable")
        events = self.journal.audit_events(execution_id)
        return [
            {
                "sequence": item["sequence"],
                "event_type": item["event_type"],
                "from_state": item["from_state"],
                "to_state": item["to_state"],
                "occurred_at": item["occurred_at"],
                "previous_hash": item["previous_hash"],
                "event_hash": item["event_hash"],
            }
            for item in events
        ]


def create_mcp_server(service: ReadOnlyMCPService | None = None) -> MCPServer:
    selected = service or ReadOnlyMCPService()
    mcp = MCPServer(
        "agent4-deployment-recovery",
        instructions=(
            "Read-only validation, deterministic planning, evidence compatibility, "
            "and operator-configured inspection for Agent 4. This server exposes no "
            "deployment, execution, rollback, trust configuration, shell, or arbitrary "
            "Kubernetes patch capability."
        ),
    )

    mcp.tool(name="get_capabilities")(selected.capabilities)
    mcp.tool(name="validate_typed_operation")(selected.validate_operation)
    mcp.tool(name="preview_deployment_plan")(selected.plan_preview)
    mcp.tool(name="check_agent3_evidence")(selected.check_agent3_evidence)
    mcp.tool(name="check_agent5_evidence")(selected.check_agent5_evidence)
    if selected.inspector is not None:
        mcp.tool(name="preflight_patch_preview")(selected.preflight_patch_preview)
    if selected.execution_access is not None:
        mcp.tool(name="get_execution_status")(selected.execution_status)
        mcp.tool(name="get_audit_events")(selected.audit_events)
    return mcp


server = create_mcp_server()


if __name__ == "__main__":
    server.run()
