"""One authoritative mapping for supported v1 events.

The first block is the v5 topic table (section 3.2) unchanged. The second block
holds the topics the shared platform adds; see ARCHITECTURE.md.
"""

from aiops_platform.contracts.actions import ActionRequest
from aiops_platform.contracts.base import Contract
from aiops_platform.contracts.enums import AgentId
from aiops_platform.contracts.platform_payloads import PlanPrepared
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.contracts.payloads import (
    ActionCompleted,
    AnomalyDetected,
    ApprovalRequested,
    ApprovalResolved,
    DiagnosisCompleted,
    ModelHealthAssessed,
    RecoveryVerified,
    ReleaseAssessment,
    ResourceRecommendation,
    WorkflowContinuation,
)

TOPICS: dict[str, tuple[str, type[Contract]]] = {
    "monitoring.anomaly.detected": ("ANOMALY_DETECTED", AnomalyDetected),
    "monitoring.recovery.verified": ("RECOVERY_VERIFIED", RecoveryVerified),
    "diagnosis.completed": ("DIAGNOSIS_COMPLETED", DiagnosisCompleted),
    "quality.release.assessed": ("RELEASE_ASSESSMENT", ReleaseAssessment),
    "deployment.action.requested": ("ACTION_REQUESTED", ActionRequest),
    "deployment.action.completed": ("ACTION_COMPLETED", ActionCompleted),
    "resource.recommendation.created": ("RESOURCE_RECOMMENDATION", ResourceRecommendation),
    "mlops.model.health.assessed": ("MODEL_HEALTH_ASSESSED", ModelHealthAssessed),
    "approval.requested": ("APPROVAL_REQUESTED", ApprovalRequested),
    "approval.resolved": ("APPROVAL_RESOLVED", ApprovalResolved),
    "workflow.continuation": ("WORKFLOW_CONTINUATION", WorkflowContinuation),
}


def task_topic(agent_id: AgentId | str) -> str:
    """Topic the Orchestrator uses to dispatch one bounded task to one agent."""
    return f"agent.task.{AgentId(agent_id).value}"


# A task outcome with no domain event: a failure, or a success with nothing to
# report (e.g. "no anomaly"). The Orchestrator reads `AgentResult.status`.
TASK_RESULT = "agent.task.result"
PLAN_PREPARED = "deployment.plan.prepared"

TOPICS.update({task_topic(agent): ("AGENT_TASK", AgentTask) for agent in AgentId})
TOPICS[TASK_RESULT] = ("AGENT_TASK_RESULT", AgentResult)
TOPICS[PLAN_PREPARED] = ("DEPLOYMENT_PLAN_PREPARED", PlanPrepared)


def retry_topic(topic: str, group: str) -> str:
    """Retries are per consumer group: a retry must not redeliver to other groups."""
    return f"{topic}.retry.{group}"


def dlq_topic(topic: str) -> str:
    return f"{topic}.dlq"
