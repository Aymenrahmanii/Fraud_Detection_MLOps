"""The shared Orchestrator as a LangGraph supervisor graph (v5 section 9).

One graph thread per workflow (`thread_id = incident_id`), checkpointed, so a
workflow survives restarts and can wait for minutes (agent results) or hours
(a human) without holding anything in memory.

Waiting is done with LangGraph `interrupt()`: a node that dispatched work
returns, then `await_events` pauses until the Orchestrator service resumes the
thread with the matching result event (`Command(resume=event)`). Side effects
(publishing tasks, Context writes) never happen in `await_events`, because a
node re-runs from its start on every resume.

Flows (v5 section 18):

* INCIDENT: anomaly -> RCA (+ resource alternative) -> consensus -> policy ->
  Agent 4 prepare -> AUTO | HITL -> execute -> Agent 1 verify -> close/replan
* RELEASE:  Agent 3 gate -> BLOCK stops | policy -> prepare -> HITL -> deploy -> verify
* MLOPS:    Agent 6 health -> retrain/evaluate -> human promotion review -> record
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aiops_platform.bus import EventBus, publish
from aiops_platform.contracts.actions import ActionIntent, ActionRequest
from aiops_platform.contracts.context import CandidateAction
from aiops_platform.contracts.enums import AgentId, Environment, PolicyDecision, Priority, Severity
from aiops_platform.contracts.events import EventEnvelope, ServiceIdentity
from aiops_platform.contracts.payloads import ApprovalRequested, ApprovalResolved, WorkflowContinuation
from aiops_platform.contracts.platform_payloads import PlanPrepared
from aiops_platform.contracts.tasks import AgentTask, TaskContext
from aiops_platform.contracts.topics import TOPICS, task_topic
from aiops_platform.hitl.service import ApprovalRequest
from aiops_platform.orchestrator.consensus import build_consensus
from aiops_platform.policy import PolicyGate, PolicyInput

log = logging.getLogger(__name__)


def approval_ttl_seconds() -> int:
    """How long a human has to decide: AIOPS_APPROVAL_TTL_SECONDS, default 600 s, at least 30 s.

    An expired approval is re-requested once, then the workflow escalates.
    """
    return max(30, int(os.environ.get("AIOPS_APPROVAL_TTL_SECONDS", "600")))


def action_fingerprint(candidate: dict[str, Any]) -> str:
    """The change itself, independent of which re-plan or agent produced it."""
    return f"{candidate['type']}:{json.dumps(candidate.get('parameters') or {}, sort_keys=True)}"


SOURCE = "orchestrator"
MAX_REPLANS = 1
MAX_APPROVAL_ROUNDS = 2
TASK_DEADLINE = timedelta(minutes=10)


class WorkflowState(TypedDict, total=False):
    incident_id: str
    kind: Literal["INCIDENT", "RELEASE", "MLOPS"]
    stage: str
    environment: str
    priority: str
    severity: str
    service: dict[str, str]
    trace_id: str
    trigger: dict[str, Any]
    signals: dict[str, Any]
    # message_id of a dispatched task/request -> what it is
    pending: dict[str, dict[str, Any]]
    after_await: str
    # domain outputs by role: monitoring, diagnosis, resource, release, model_health, lifecycle, plan, action, verification
    results: dict[str, Any]
    candidates: list[dict[str, Any]]
    tried: list[str]
    # what a human rejected (action type + parameters), so a re-plan never asks again
    rejected: list[str]
    consensus: dict[str, Any]
    selected: dict[str, Any] | None
    policy: dict[str, Any] | None
    approval: dict[str, Any] | None
    approval_rounds: int
    replans: int
    outcome: str | None
    notes: list[str]


class Deps:
    """Everything the nodes need; built by `OrchestratorService`."""

    def __init__(
        self,
        *,
        bus: EventBus,
        context,
        hitl,
        policy: PolicyGate | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bus = bus
        self.context = context  # OrchestratorContext
        self.hitl = hitl  # HitlService (or gRPC client)
        self.policy = policy or PolicyGate()
        self.clock = clock or (lambda: datetime.now(UTC))


def _service(state: WorkflowState) -> ServiceIdentity:
    return ServiceIdentity(**state["service"])


def _envelope(
    state: WorkflowState, *, topic: str, payload: dict[str, Any], target: str, causation_id: str | None = None, key: str
) -> EventEnvelope:
    return EventEnvelope(
        correlation_id=state["incident_id"],
        causation_id=causation_id,
        trace_id=state["trace_id"],
        source=SOURCE,
        target=target,
        topic=topic,
        message_type=TOPICS[topic][0],
        priority=Priority(state["priority"]),
        severity=Severity(state["severity"]),
        deadline=datetime.now(UTC) + TASK_DEADLINE,
        environment=Environment(state["environment"]),
        service=_service(state),
        idempotency_key=f"{state['incident_id']}:{TOPICS[topic][0]}:{key}",
        payload=payload,
    )


class WorkflowNodes:
    def __init__(self, deps: Deps) -> None:
        self.d = deps

    # ---- helpers ---------------------------------------------------------

    async def _dispatch(
        self,
        state: WorkflowState,
        agent: AgentId,
        objective: str,
        signals: dict[str, Any],
        *,
        role: str,
        constraints: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
    ) -> tuple[str, dict[str, Any]]:
        """Publish one bounded task (v5 section 6.4); returns (message_id, pending entry)."""
        svc = state["service"]
        task = AgentTask(
            task_id=f"TASK-{agent.value.split('-')[0].upper()}-{uuid4().hex[:10]}",
            incident_id=state["incident_id"],
            agent_id=agent,
            objective=objective,
            task_context=TaskContext(
                service_id=svc["service_id"],
                namespace=svc["namespace"],
                environment=Environment(state["environment"]),
                signals=signals,
                evidence_refs=evidence_refs,
                constraints=constraints,
            ),
            deadline=datetime.now(UTC) + TASK_DEADLINE,
        )
        envelope = _envelope(
            state, topic=task_topic(agent), payload=task.model_dump(mode="json"), target=agent.value, key=task.task_id
        )
        await publish(self.d.bus, envelope)
        return envelope.message_id, {
            "role": role,
            "agent": agent.value,
            "objective": objective,
            "task_id": task.task_id,
        }

    async def _stage(self, state: WorkflowState, stage: str) -> None:
        await self.d.context.update_incident(state["incident_id"], status=stage)

    # ---- ingest / route --------------------------------------------------

    async def ingest(self, state: WorkflowState) -> WorkflowState:
        trigger = state["trigger"]
        svc = trigger["service"]
        ctx, created = await self.d.context.open_incident(
            incident_id=state["incident_id"],
            kind=state["kind"],
            service=ServiceIdentity(**svc),
            environment=Environment(state["environment"]),
            severity=Severity(state["severity"]),
            priority=Priority(state["priority"]),
            signals=state.get("signals", {}),
        )
        results = dict(state.get("results", {}))
        if state["kind"] == "INCIDENT":
            results["monitoring"] = {"payload": trigger.get("payload", {}), "confidence": trigger.get("confidence")}
        return {
            "stage": "OPEN",
            "results": results,
            "pending": {},
            "candidates": [],
            "tried": [],
            "replans": 0,
            "approval_rounds": 0,
            "notes": [f"workflow opened (context v{ctx.version})"],
        }

    def route_kind(self, state: WorkflowState) -> str:
        return {"INCIDENT": "dispatch_rca", "RELEASE": "dispatch_release", "MLOPS": "dispatch_model_health"}[
            state["kind"]
        ]

    # ---- await (pure: no side effects) -----------------------------------

    def await_events(self, state: WorkflowState) -> WorkflowState:
        pending = dict(state.get("pending", {}))
        results = dict(state.get("results", {}))
        notes = list(state.get("notes", []))
        while pending:
            event = interrupt({"waiting_for": sorted(pending), "roles": sorted(p["role"] for p in pending.values())})
            key = event.get("causation_id")
            if key not in pending:
                continue  # unrelated or duplicate event: keep waiting
            entry = pending.pop(key)
            role = entry["role"]
            if event.get("topic") == "timeout":
                results[role] = {"status": "TIMEOUT", "payload": {}}
                notes.append(f"{entry['agent']} timed out on {entry['objective']}")
                continue
            payload = event.get("payload", {})
            if event.get("topic") == "agent.task.result":
                results[role] = {
                    "status": payload.get("status"),
                    "payload": payload.get("payload", {}),
                    "summary": payload.get("rationale_summary"),
                    "confidence": payload.get("confidence"),
                }
            else:
                results[role] = {
                    "status": "SUCCEEDED",
                    "payload": payload,
                    "confidence": event.get("confidence"),
                    "message_id": event.get("message_id"),
                }
        return {"pending": {}, "results": results, "notes": notes}

    def route_after_await(self, state: WorkflowState) -> str:
        return state["after_await"]

    # ---- INCIDENT: diagnosis ----------------------------------------------

    async def dispatch_rca(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "DIAGNOSING")
        anomaly = state["results"].get("monitoring", {}).get("payload", {})
        signals = dict(anomaly.get("signals", {}))
        replan = state.get("results", {}).get("replan_request")
        constraints = ("do_not_execute_side_effects", "return_ranked_hypotheses")
        if replan:
            signals["required_evidence"] = list(replan.get("required_evidence", []))
        pending = {}
        mid, entry = await self._dispatch(
            state,
            AgentId.DIAGNOSIS,
            "identify_root_cause_and_recommend_action",
            signals,
            role="diagnosis",
            constraints=constraints,
        )
        pending[mid] = entry
        # Independent branch in parallel (v5 9: "parallel branches when independent").
        mid, entry = await self._dispatch(
            state,
            AgentId.RESOURCE_OPTIMIZATION,
            "recommend_resources",
            signals,
            role="resource",
            constraints=("recommend_only",),
        )
        pending[mid] = entry
        return {"pending": pending, "after_await": "consensus", "stage": "DIAGNOSING"}

    async def consensus(self, state: WorkflowState) -> WorkflowState:
        r = state["results"]
        diagnosis = r.get("diagnosis", {})
        resource = r.get("resource", {})
        c = build_consensus(
            diagnosis.get("payload") if diagnosis.get("status") == "SUCCEEDED" else None,
            resource.get("payload")
            if resource.get("status") == "SUCCEEDED" and resource.get("payload", {}).get("candidate_action")
            else None,
            monitoring_confidence=r.get("monitoring", {}).get("confidence"),
        )
        for cand in c.candidates:
            await self.d.context.record_action(
                state["incident_id"],
                CandidateAction(
                    action_id=cand.action_id,
                    action_version=cand.action_version,
                    type=cand.type,
                    source_agent=cand.source_agent,
                    score=round(cand.score, 4),
                    parameters=cand.parameters,
                ),
            )
        await self.d.context.record_decision(
            state["incident_id"], stage="CONSENSUS", decision_type="CONSENSUS", payload=c.as_json()
        )
        return {"consensus": c.as_json(), "candidates": [cand.as_json() for cand in c.candidates], "stage": "DECIDING"}

    # ---- policy -------------------------------------------------------------

    async def policy_gate(self, state: WorkflowState) -> WorkflowState:
        tried = set(state.get("tried", []))
        rejected = set(state.get("rejected", []))
        fresh = [c for c in state.get("candidates", []) if c["action_id"] not in tried]
        # A re-plan gets new action IDs; a change a human already rejected is never proposed again.
        candidates = [c for c in fresh if action_fingerprint(c) not in rejected]
        if not candidates:
            outcome = "REJECTED_BY_HUMAN" if fresh else "NO_SAFE_ACTION"
            notes = list(state.get("notes", []))
            if fresh:
                notes.append(f"no new option: a human already rejected {', '.join(sorted({c['type'] for c in fresh}))}")
            return {"selected": None, "policy": None, "outcome": outcome, "notes": notes}
        cand = candidates[0]
        c = state.get("consensus", {})
        release = state.get("results", {}).get("release", {}).get("payload", {})
        decision = self.d.policy.evaluate(
            PolicyInput(
                action=cand["type"],
                environment=state["environment"],
                parameters=cand["parameters"],
                rca_confidence=c.get("rca_confidence", 1.0 if state["kind"] == "RELEASE" else 0.0),
                evidence_strength=c.get("evidence_strength", 1.0 if state["kind"] == "RELEASE" else 0.5),
                agent_agreement=c.get("agreement", 1.0),
                historical_success=None,
                evidence_complete=state["kind"] != "INCIDENT" or bool(state["results"].get("diagnosis")),
                release_gate=release.get("decision") if state["kind"] == "RELEASE" else None,
            )
        )
        await self.d.context.update_action(
            state["incident_id"],
            cand["action_id"],
            cand["action_version"],
            policy_result=decision.decision.value,
            risk=decision.as_json()["risk"],
        )
        await self.d.context.record_decision(
            state["incident_id"],
            stage="POLICY",
            decision_type=decision.decision.value,
            payload={"action_id": cand["action_id"], **decision.as_json()},
        )
        return {"selected": cand, "policy": decision.as_json(), "tried": [*tried, cand["action_id"]]}

    def route_policy(self, state: WorkflowState) -> str:
        if state.get("selected") is None:
            return "escalate"
        if state["policy"]["decision"] == PolicyDecision.BLOCK.value:
            # A blocked candidate: try the next one, if any.
            remaining = [c for c in state["candidates"] if c["action_id"] not in state["tried"]]
            return "policy_gate" if remaining else "escalate"
        return "prepare_action"

    # ---- Agent 4 prepare ---------------------------------------------------

    async def prepare_action(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "PLANNING")
        cand = state["selected"]
        mid, entry = await self._dispatch(
            state,
            AgentId.DEPLOYMENT_RECOVERY,
            "prepare_action",
            {
                "action_id": cand["action_id"],
                "action_version": cand["action_version"],
                "action": cand["type"],
                "parameters": cand["parameters"],
            },
            role="plan",
            constraints=("dry_run_only",),
        )
        return {"pending": {mid: entry}, "after_await": "route_plan", "stage": "PLANNING"}

    async def route_plan_node(self, state: WorkflowState) -> WorkflowState:
        plan = state["results"].get("plan", {}).get("payload", {})
        cand = state["selected"]
        if plan.get("status") != "PREPARED":
            await self.d.context.update_action(
                state["incident_id"], cand["action_id"], cand["action_version"], execution_status="PLAN_REJECTED"
            )
            return {"notes": [*state.get("notes", []), f"Agent 4 rejected {cand['type']}: {plan.get('reason')}"]}
        return {}

    def route_plan(self, state: WorkflowState) -> str:
        plan = state["results"].get("plan", {}).get("payload", {})
        if plan.get("status") == "PREPARED":
            return "authorize"
        remaining = [c for c in state["candidates"] if c["action_id"] not in state["tried"]]
        return "policy_gate" if remaining else "escalate"

    # ---- authorize: AUTO permit or human approval ---------------------------

    def _approval_request(self, state: WorkflowState, plan: dict[str, Any] | None) -> ApprovalRequest:
        cand = state["selected"]
        policy = state.get("policy") or {}
        rounds = state.get("approval_rounds", 0) + 1
        r = state["results"]
        evidence = []
        diag = r.get("diagnosis", {}).get("payload", {})
        if diag.get("rationale_summary"):
            evidence.append(str(diag["rationale_summary"]))
        signals = r.get("monitoring", {}).get("payload", {}).get("signals", {})
        for key in ("memory_pct", "memory_slope_mb_min", "error_rate", "latency_p95_ms", "pod_restarts", "cpu_avg_pct"):
            if key in signals:
                evidence.append(f"{key}={signals[key]}")
        if r.get("release"):
            evidence.append(f"release gate {r['release']['payload'].get('decision')}")
        if r.get("lifecycle"):
            evidence.append(str(r["lifecycle"]["payload"].get("rationale_summary", "")))
        return ApprovalRequest(
            approval_id=f"APR-{state['incident_id']}-{cand['action_id']}-{rounds}"[:128],
            incident_id=state["incident_id"],
            correlation_id=state["incident_id"],
            environment=state["environment"],
            priority=state["priority"],
            severity=state["severity"],
            action_id=cand["action_id"],
            action_version=cand["action_version"],
            action=cand["type"],
            parameters=cand["parameters"],
            risk=policy.get("risk", {}),
            decision_confidence=float(policy.get("decision_score", 0.0)),
            reason=cand.get("rationale", ""),
            evidence_summary=tuple(evidence),
            hitl_triggers=tuple(policy.get("reasons", [])),
            plan=PlanPrepared.model_validate(plan) if plan else None,
            ttl_seconds=approval_ttl_seconds(),
        )

    async def authorize(self, state: WorkflowState) -> WorkflowState:
        plan = state["results"].get("plan", {}).get("payload")
        request = self._approval_request(state, plan)
        if state["policy"]["decision"] == PolicyDecision.AUTO.value:
            approval = await self.d.hitl.issue_policy_permit(request)
            return {"approval": approval.model_dump(mode="json"), "stage": "AUTHORIZED"}
        await self._stage(state, "WAITING_FOR_APPROVAL")
        approval = await self.d.hitl.create_approval(request)
        cand = state["selected"]
        intent = ActionIntent.model_validate(_intent(state, cand))
        await publish(
            self.d.bus,
            _envelope(
                state,
                topic="approval.requested",
                target="dashboard-ui",
                key=request.approval_id,
                payload=ApprovalRequested(
                    approval_id=request.approval_id,
                    intent=intent,
                    action_fingerprint=intent.fingerprint(),
                    reason=request.reason,
                    expires_at=approval.expires_at,
                ).model_dump(mode="json"),
            ),
        )
        return {
            "approval": approval.model_dump(mode="json"),
            "approval_rounds": state.get("approval_rounds", 0) + 1,
            "stage": "WAITING_FOR_APPROVAL",
        }

    def route_authorize(self, state: WorkflowState) -> str:
        return "execute" if state["approval"]["state"] == "APPROVED" else "await_human"

    def await_human(self, state: WorkflowState) -> WorkflowState:
        approval = dict(state["approval"])
        while True:
            event = interrupt({"waiting_for_approval": approval["approval_id"], "expires_at": approval["expires_at"]})
            if event.get("topic") == "hitl" and event.get("approval_id") == approval["approval_id"]:
                return {"approval": event["approval"]}

    async def after_human(self, state: WorkflowState) -> WorkflowState:
        approval = state["approval"]
        await self.d.context.record_hitl_decision(
            state["incident_id"],
            approval_id=approval["approval_id"],
            decision=approval["state"],
            approver_id=approval.get("approver_id"),
            comment=approval.get("comment") or "",
        )
        cand = state["selected"]
        await publish(
            self.d.bus,
            _envelope(
                state,
                topic="approval.resolved",
                target="audit",
                key=f"{approval['approval_id']}:{approval['state']}",
                payload=ApprovalResolved(
                    approval_id=approval["approval_id"],
                    action_id=cand["action_id"],
                    action_version=cand["action_version"],
                    decision=approval["state"],
                    approver_id=approval.get("approver_id"),
                    comment=approval.get("comment") or "",
                ).model_dump(mode="json"),
            ),
        )
        if approval["state"] == "REJECTED":
            await self.d.context.update_action(
                state["incident_id"], cand["action_id"], cand["action_version"], execution_status="REJECTED_BY_HUMAN"
            )
            return {
                "stage": f"HITL_{approval['state']}",
                "rejected": [*state.get("rejected", []), action_fingerprint(cand)],
            }
        return {"stage": f"HITL_{approval['state']}"}

    def route_human(self, state: WorkflowState) -> str:
        approval = state["approval"]
        if approval["state"] == "APPROVED":
            return "close_mlops" if state["kind"] == "MLOPS" else "execute"
        if approval["state"] == "EXPIRED":
            # v5 7.2: no execution; re-request only if still valid and within budget.
            return (
                "authorize"
                if state.get("approval_rounds", 0) < MAX_APPROVAL_ROUNDS and state["kind"] != "MLOPS"
                else "escalate"
            )
        if state["kind"] == "MLOPS":
            return "close_mlops"
        return "replan" if state.get("replans", 0) < MAX_REPLANS and state["kind"] == "INCIDENT" else "escalate"

    # ---- execute / verify ----------------------------------------------------

    async def execute(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "EXECUTING")
        cand = state["selected"]
        approval = state["approval"]
        auto = state["policy"]["decision"] == PolicyDecision.AUTO.value
        request = ActionRequest(
            intent=ActionIntent.model_validate(_intent(state, cand)),
            policy_decision=PolicyDecision.AUTO if auto else PolicyDecision.APPROVAL_REQUIRED,
            approval_id=approval["approval_id"],
            approval_token=approval["approval_token"],
            idempotency_key=f"{state['incident_id']}:{cand['action_id']}:v{cand['action_version']}",
        )
        if auto:
            envelope = _envelope(
                state,
                topic="deployment.action.requested",
                target=AgentId.DEPLOYMENT_RECOVERY.value,
                key=cand["action_id"],
                payload=request.model_dump(mode="json"),
            )
        else:
            envelope = _envelope(
                state,
                topic="workflow.continuation",
                target=AgentId.DEPLOYMENT_RECOVERY.value,
                key=cand["action_id"],
                payload=WorkflowContinuation(
                    incident_id=state["incident_id"],
                    next_step="EXECUTE_ACTION",
                    action_request=request,
                    reason="human approved the exact plan",
                ).model_dump(mode="json"),
            )
        await publish(self.d.bus, envelope)
        await self.d.context.update_action(
            state["incident_id"], cand["action_id"], cand["action_version"], execution_status="EXECUTING"
        )
        return {
            "pending": {
                envelope.message_id: {
                    "role": "action",
                    "agent": AgentId.DEPLOYMENT_RECOVERY.value,
                    "objective": "execute",
                }
            },
            "after_await": "after_execute",
            "stage": "EXECUTING",
        }

    async def after_execute(self, state: WorkflowState) -> WorkflowState:
        action = state["results"].get("action", {}).get("payload", {})
        cand = state["selected"]
        await self.d.context.update_action(
            state["incident_id"],
            cand["action_id"],
            cand["action_version"],
            execution_status=action.get("status", "UNKNOWN"),
        )
        return {}

    def route_execute(self, state: WorkflowState) -> str:
        return (
            "verify" if state["results"].get("action", {}).get("payload", {}).get("status") == "SUCCESS" else "escalate"
        )

    async def verify(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "VERIFYING")
        cand = state["selected"]
        categories = (
            state["results"]
            .get("monitoring", {})
            .get("payload", {})
            .get("signals", {})
            .get("categories", ["error_rate", "latency"])
        )
        mid, entry = await self._dispatch(
            state,
            AgentId.MONITORING,
            "verify_recovery",
            {"action_id": cand["action_id"], "categories": categories},
            role="verification",
        )
        return {"pending": {mid: entry}, "after_await": "after_verify", "stage": "VERIFYING"}

    async def after_verify(self, state: WorkflowState) -> WorkflowState:
        verdict = state["results"].get("verification", {}).get("payload", {}).get("status", "INCONCLUSIVE")
        cand = state["selected"]
        await self.d.context.update_action(
            state["incident_id"],
            cand["action_id"],
            cand["action_version"],
            execution_status="VERIFIED" if verdict == "RECOVERED" else f"VERIFY_{verdict}",
        )
        return {"outcome": verdict}

    def route_verify(self, state: WorkflowState) -> str:
        if state.get("outcome") == "RECOVERED":
            return "close"
        return "replan" if state.get("replans", 0) < MAX_REPLANS and state["kind"] == "INCIDENT" else "escalate"

    async def replan(self, state: WorkflowState) -> WorkflowState:
        """v5 17.1: ask for targeted evidence and re-run only the affected branch."""
        replans = state.get("replans", 0) + 1
        results = dict(state["results"])
        results["replan_request"] = {
            "required_evidence": ["heap_usage_breakdown", "gc_metrics", "pod_status", "events"]
        }
        for role in ("diagnosis", "resource", "plan", "action", "verification"):
            results.pop(role, None)
        await self.d.context.record_decision(
            state["incident_id"],
            stage="REPLAN",
            decision_type="REPLAN",
            payload={"replan": replans, "reason": state.get("stage")},
        )
        return {"results": results, "replans": replans, "approval_rounds": 0, "stage": "REPLANNING"}

    # ---- closing -------------------------------------------------------------

    async def close(self, state: WorkflowState) -> WorkflowState:
        await self.d.context.record_decision(
            state["incident_id"],
            stage="CLOSE",
            decision_type="RESOLVED",
            payload={"outcome": state.get("outcome") or "DONE", "action": state.get("selected")},
        )
        await self.d.context.update_incident(state["incident_id"], status="RESOLVED", closed=True)
        return {"stage": "RESOLVED"}

    async def escalate(self, state: WorkflowState) -> WorkflowState:
        reason = state.get("outcome") or state.get("stage") or "no safe action"
        if state.get("policy", {}) and state["policy"].get("decision") == "BLOCK":
            reason = "; ".join(state["policy"].get("reasons", [])) or reason
        await self.d.context.record_decision(
            state["incident_id"],
            stage="CLOSE",
            decision_type="ESCALATED",
            payload={"reason": reason, "notes": state.get("notes", [])[-5:]},
        )
        await self.d.context.update_incident(state["incident_id"], status="ESCALATED")
        return {"stage": "ESCALATED"}

    # ---- RELEASE flow --------------------------------------------------------

    async def dispatch_release(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "ASSESSING_RELEASE")
        mid, entry = await self._dispatch(
            state, AgentId.SECURITY_QUALITY, "assess_release", dict(state.get("signals", {})), role="release"
        )
        return {"pending": {mid: entry}, "after_await": "release_gate", "stage": "ASSESSING_RELEASE"}

    async def release_gate(self, state: WorkflowState) -> WorkflowState:
        release = state["results"].get("release", {})
        decision = (
            release.get("payload", {}).get("decision", "BLOCK") if release.get("status") == "SUCCEEDED" else "BLOCK"
        )
        await self.d.context.record_decision(
            state["incident_id"],
            stage="RELEASE_GATE",
            decision_type=decision,
            payload={"assessment": release.get("payload", {})},
        )
        if decision == "BLOCK":
            return {"outcome": "RELEASE_BLOCKED", "candidates": []}
        version = state["signals"].get("version")
        cand = {
            "action_id": f"ACT-DEPLOY-{state['incident_id']}"[:120],
            "action_version": 1,
            "type": "DEPLOY",
            "source_agent": "security-quality-agent",
            "score": 1.0,
            "parameters": {"version": version},
            "rationale": f"release {version} passed the Agent 3 gate ({decision})",
            "intent": {},
        }
        await self.d.context.record_action(
            state["incident_id"],
            CandidateAction(
                action_id=cand["action_id"],
                type="DEPLOY",
                source_agent=cand["source_agent"],
                score=1.0,
                parameters=cand["parameters"],
            ),
        )
        return {"candidates": [cand], "consensus": {"rca_confidence": 1.0, "evidence_strength": 1.0, "agreement": 1.0}}

    def route_release(self, state: WorkflowState) -> str:
        return "escalate_release" if state.get("outcome") == "RELEASE_BLOCKED" else "policy_gate"

    async def escalate_release(self, state: WorkflowState) -> WorkflowState:
        await self.d.context.update_incident(state["incident_id"], status="RELEASE_BLOCKED", closed=True)
        return {"stage": "RELEASE_BLOCKED"}

    # ---- MLOPS flow ----------------------------------------------------------

    async def dispatch_model_health(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "ASSESSING_MODEL")
        mid, entry = await self._dispatch(
            state, AgentId.MLOPS_LIFECYCLE, "assess_model_health", dict(state.get("signals", {})), role="model_health"
        )
        return {"pending": {mid: entry}, "after_await": "model_decision", "stage": "ASSESSING_MODEL"}

    async def model_decision(self, state: WorkflowState) -> WorkflowState:
        health = state["results"].get("model_health", {}).get("payload", {})
        await self.d.context.record_model_event(
            state["incident_id"],
            model_name=health.get("model_name", "unknown"),
            model_version=health.get("model_version", "unknown"),
            event_type="MODEL_HEALTH_ASSESSED",
            status=health.get("decision", "UNKNOWN"),
            metrics={"drift_detected": health.get("drift_detected"), "drift_score": health.get("drift_score")},
        )
        return {"outcome": health.get("decision")}

    def route_model(self, state: WorkflowState) -> str:
        return "dispatch_retrain" if state.get("outcome") == "RETRAIN_AND_EVALUATE" else "close_mlops"

    async def dispatch_retrain(self, state: WorkflowState) -> WorkflowState:
        await self._stage(state, "RETRAINING")
        health = state["results"]["model_health"]["payload"]
        mid, entry = await self._dispatch(
            state,
            AgentId.MLOPS_LIFECYCLE,
            "retrain_and_evaluate",
            {"dataset_ref": health.get("dataset_ref"), "reason": "drift_and_performance_drop"},
            role="lifecycle",
        )
        return {"pending": {mid: entry}, "after_await": "promotion_review", "stage": "RETRAINING"}

    async def promotion_review(self, state: WorkflowState) -> WorkflowState:
        lifecycle = state["results"].get("lifecycle", {}).get("payload", {})
        await self.d.context.record_model_event(
            state["incident_id"],
            model_name=lifecycle.get("model_name", "unknown"),
            model_version=str(lifecycle.get("registered_model_version")),
            event_type="CANDIDATE_EVALUATED",
            status=lifecycle.get("decision", "UNKNOWN"),
            metrics={"candidate": lifecycle.get("candidate"), "champion": lifecycle.get("champion")},
        )
        if lifecycle.get("decision") != "REQUEST_PROMOTION_REVIEW":
            return {"outcome": lifecycle.get("decision", "KEEP_CHAMPION"), "selected": None}
        cand = {
            "action_id": f"ACT-PROMOTE-{state['incident_id']}"[:120],
            "action_version": 1,
            "type": "PROMOTE_MODEL",
            "source_agent": "mlops-lifecycle-agent",
            "score": 1.0,
            "parameters": {
                "model_name": lifecycle.get("model_name"),
                "version": lifecycle.get("registered_model_version"),
                "run_id": lifecycle.get("run_id"),
            },
            "rationale": lifecycle.get("rationale_summary", ""),
            "intent": {},
        }
        policy = {
            "decision": "APPROVAL_REQUIRED",
            "decision_score": 1.0,
            "risk": {"level": "MEDIUM"},
            "reasons": ["model promotion requires human review (v5 18.1)"],
        }
        await self._stage(state, "WAITING_FOR_APPROVAL")
        approval = await self.d.hitl.create_approval(
            self._approval_request({**state, "selected": cand, "policy": policy}, None)
        )
        return {
            "selected": cand,
            "policy": policy,
            "approval": approval.model_dump(mode="json"),
            "approval_rounds": 1,
            "stage": "WAITING_FOR_APPROVAL",
        }

    def route_promotion(self, state: WorkflowState) -> str:
        return "await_human" if state.get("selected") else "close_mlops"

    async def close_mlops(self, state: WorkflowState) -> WorkflowState:
        approval = state.get("approval") or {}
        outcome = state.get("outcome")
        if approval.get("state") == "APPROVED":
            outcome = "PROMOTION_APPROVED"
        elif approval.get("state") in ("REJECTED", "EXPIRED"):
            outcome = "KEEP_CHAMPION"
        if state.get("selected"):
            p = state["selected"]["parameters"]
            await self.d.context.record_model_event(
                state["incident_id"],
                model_name=str(p.get("model_name")),
                model_version=str(p.get("version")),
                event_type="PROMOTION_DECISION",
                status=outcome or "UNKNOWN",
                metrics={"approver": approval.get("approver_id")},
            )
        await self.d.context.record_decision(
            state["incident_id"],
            stage="CLOSE",
            decision_type=outcome or "DONE",
            payload={"approval": approval.get("approval_id")},
        )
        await self.d.context.update_incident(state["incident_id"], status=outcome or "CLOSED", closed=True)
        return {"stage": outcome or "CLOSED", "outcome": outcome}


def _intent(state: WorkflowState, cand: dict[str, Any]) -> dict[str, Any]:
    if cand.get("intent"):
        intent = dict(cand["intent"])
        intent.setdefault("action_version", cand["action_version"])
        return intent
    svc = state["service"]
    return {
        "action_id": cand["action_id"],
        "action_version": cand["action_version"],
        "incident_id": state["incident_id"],
        "action": cand["type"],
        "environment": state["environment"],
        "target": {"service": svc["service_id"], "namespace": svc["namespace"]},
        "parameters": cand["parameters"],
        "verification_profile": f"{svc['service_id']}-slo-default",
    }


def build_graph(deps: Deps, checkpointer):
    n = WorkflowNodes(deps)
    g = StateGraph(WorkflowState)
    for name in (
        "ingest",
        "dispatch_rca",
        "consensus",
        "policy_gate",
        "prepare_action",
        "authorize",
        "execute",
        "after_execute",
        "verify",
        "after_verify",
        "replan",
        "close",
        "escalate",
        "after_human",
        "dispatch_release",
        "release_gate",
        "escalate_release",
        "dispatch_model_health",
        "model_decision",
        "dispatch_retrain",
        "promotion_review",
        "close_mlops",
    ):
        g.add_node(name, getattr(n, name))
    g.add_node("await_events", n.await_events)
    g.add_node("await_human", n.await_human)
    g.add_node("route_plan", n.route_plan_node)

    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", n.route_kind, ["dispatch_rca", "dispatch_release", "dispatch_model_health"])
    for dispatcher in (
        "dispatch_rca",
        "prepare_action",
        "execute",
        "verify",
        "dispatch_release",
        "dispatch_model_health",
        "dispatch_retrain",
    ):
        g.add_edge(dispatcher, "await_events")
    g.add_conditional_edges(
        "await_events",
        n.route_after_await,
        [
            "consensus",
            "route_plan",
            "after_execute",
            "after_verify",
            "release_gate",
            "model_decision",
            "promotion_review",
        ],
    )
    g.add_edge("consensus", "policy_gate")
    g.add_conditional_edges("policy_gate", n.route_policy, ["prepare_action", "policy_gate", "escalate"])
    g.add_conditional_edges("route_plan", n.route_plan, ["authorize", "policy_gate", "escalate"])
    g.add_conditional_edges("authorize", n.route_authorize, ["execute", "await_human"])
    g.add_edge("await_human", "after_human")
    g.add_conditional_edges("after_human", n.route_human, ["execute", "authorize", "replan", "escalate", "close_mlops"])
    g.add_conditional_edges("after_execute", n.route_execute, ["verify", "escalate"])
    g.add_conditional_edges("after_verify", n.route_verify, ["close", "replan", "escalate"])
    g.add_edge("replan", "dispatch_rca")
    g.add_conditional_edges("release_gate", n.route_release, ["policy_gate", "escalate_release"])
    g.add_conditional_edges("model_decision", n.route_model, ["dispatch_retrain", "close_mlops"])
    g.add_conditional_edges("promotion_review", n.route_promotion, ["await_human", "close_mlops"])
    for terminal in ("close", "escalate", "escalate_release", "close_mlops"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer)
