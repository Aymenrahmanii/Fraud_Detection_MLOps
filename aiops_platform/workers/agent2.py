"""Agent 2 (Diagnosis & RCA) worker.

Runs Agent 2's `DiagnosisAgent` (deterministic signatures + local RAG over its
own runbooks/incidents) on the Orchestrator's task. Agent 2 fetches its own
evidence through its tool allowlist; in simulation its real `KubernetesTool`
gets a `kubectl get ... -o json` runner backed by the simulated cluster, so
Agent 2's own parsing of pods, events and rollout history is exercised.
"""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from agent2_diagnosis.contracts import DiagnosisAgentResult
from agent2_diagnosis.rag_retriever import LocalKnowledgeRetriever
from agent2_diagnosis.service import DiagnosisAgent
from agent2_diagnosis.shared_contracts.tasks import AgentTask as A2Task
from agent2_diagnosis.tools import KubernetesTool, Toolbox, build_toolbox_from_env
from aiops_platform.contracts.enums import AgentId
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.tasks import AgentResult, AgentTask
from aiops_platform.llm.investigator import RulesView, apply_review
from aiops_platform.workers.base import AgentWorker, WorkerOutput


def sim_kubectl_runner(cluster):
    """`kubectl get <resource> -n <ns> ... -o json` answered from the simulated cluster."""

    def at(minute: float) -> str:
        return (datetime.now(UTC) - timedelta(minutes=cluster.minute - minute)).isoformat()

    def run(argv: Sequence[str], timeout: float) -> str:
        del timeout
        args = list(argv)
        resource = args[args.index("get") + 1]
        namespace = args[args.index("-n") + 1]
        selector = args[args.index("-l") + 1] if "-l" in args else ""
        service = selector.split("=", 1)[1] if "=" in selector else None
        deps = [cluster.deployment(service, namespace)] if service else []
        items = []
        if resource == "replicasets":
            for dep in deps:
                for revision, (minute, version) in enumerate(dep.history, start=1):
                    items.append(
                        {
                            "metadata": {
                                "name": f"{dep.service_id}-{revision}",
                                "creationTimestamp": at(minute),
                                "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                            },
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {"image": dep.releases[version].image.split("@")[0] + ":" + version}
                                        ]
                                    }
                                }
                            },
                        }
                    )
        elif resource == "pods":
            for dep in deps:
                for index in range(dep.replicas):
                    status = {"restartCount": dep.restarts, "state": {"running": {}}}
                    if dep.restarts:
                        status["lastState"] = {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
                    items.append(
                        {"metadata": {"name": f"{dep.service_id}-{index}"}, "status": {"containerStatuses": [status]}}
                    )
        elif resource == "events":
            for dep in cluster.deployments():
                if dep.namespace == namespace and dep.restarts:
                    items.append(
                        {
                            "involvedObject": {"name": f"{dep.service_id}-0"},
                            "reason": "OOMKilling",
                            "message": f"Memory cgroup out of memory: Killed process ({dep.service_id})",
                        }
                    )
        return json.dumps({"items": items})

    return run


def sim_toolbox(cluster) -> Toolbox:
    return Toolbox([KubernetesTool(runner=sim_kubectl_runner(cluster))])


def live_toolbox() -> Toolbox | None:
    """Agent 2's own live tools from its PIDS_* environment variables."""
    return build_toolbox_from_env()


class DiagnosisWorker(AgentWorker):
    agent_id = AgentId.DIAGNOSIS

    def __init__(self, bus, context, *, toolbox: Toolbox | None = None, investigator=None, **kwargs) -> None:
        super().__init__(bus, context, **kwargs)
        self._agent = DiagnosisAgent(retriever=LocalKnowledgeRetriever(), toolbox=toolbox)
        # Optional LLM review of the rules' diagnosis (aiops_platform/llm). None = rules only.
        self._investigator = investigator

    async def run(self, task: AgentTask, request: EventEnvelope) -> WorkerOutput:
        # Agent 2 is synchronous (tool calls, retrieval): keep the event loop free.
        result = await asyncio.to_thread(self._diagnose, A2Task.model_validate(task.model_dump(mode="json")))
        if not isinstance(result, DiagnosisAgentResult):
            return WorkerOutput(result=AgentResult.model_validate(result.model_dump(mode="json")))
        diagnosis = result.diagnosis.model_dump(mode="json")
        payload = {**diagnosis, "evidence_strength": result.evidence_strength}
        investigated: list[dict] = []
        review: dict | None = None
        if self._investigator is not None and payload.get("primary_root_cause"):
            hypotheses = payload.get("hypotheses") or []
            rules = RulesView(
                primary=payload["primary_root_cause"],
                candidates=tuple(h["code"] for h in hypotheses),
                probabilities={h["code"]: float(h["probability"]) for h in hypotheses},
                signals=dict(task.task_context.signals),
            )
            investigation = await self._investigator.investigate(
                service_id=task.task_context.service_id,
                namespace=task.task_context.namespace,
                rules=rules,
            )
            payload = apply_review(payload, investigation)
            # The Orchestrator's consensus reads the diagnosis event, so the review must reach it too.
            diagnosis = {
                **diagnosis,
                "requires_additional_evidence": payload["requires_additional_evidence"],
                "limitations": payload["limitations"],
            }
            investigated = investigation.tool_calls
            review = payload["llm_investigation"]
        evidence_refs: list[str] = []
        for item in result.evidence:
            ref = await self.context.append_evidence(
                incident_id=task.incident_id,
                evidence_type=item.evidence_type,
                summary=item.summary,
                content=item.model_dump(mode="json"),
                observed_at=item.observed_at or datetime.now(UTC),
            )
            evidence_refs.append(ref.evidence_id)
        # What the model looked at is evidence too: it lands in the hash-chained audit trail.
        for call in investigated:
            ref = await self.context.append_evidence(
                incident_id=task.incident_id,
                evidence_type=f"llm_tool:{call['name']}",
                summary=f"{call['name']} for {task.task_context.service_id}",
                content={"tool": call["name"], "result": call["result"]},
                observed_at=datetime.now(UTC),
            )
            evidence_refs.append(ref.evidence_id)
        if review is not None:
            # The review itself is evidence in the audit trail. The diagnosis event cannot carry it (contracts forbid extra fields).
            ref = await self.context.append_evidence(
                incident_id=task.incident_id,
                evidence_type="llm_review",
                summary=f"LLM review: {review['status']}, root cause {review['llm_root_cause']}",
                content=review,
                observed_at=datetime.now(UTC),
            )
            evidence_refs.append(ref.evidence_id)
        platform_result = AgentResult(
            result_id=result.result_id,
            task_id=result.task_id,
            incident_id=result.incident_id,
            agent_id=AgentId.DIAGNOSIS,
            status=result.status.value,
            rationale_summary=result.rationale_summary,
            evidence_refs=tuple(evidence_refs) or result.evidence_refs,
            confidence=result.confidence,
            payload=payload,
        )
        return WorkerOutput(
            result=platform_result,
            events=[("diagnosis.completed", diagnosis, result.confidence, tuple(evidence_refs))],
        )

    def _diagnose(self, task: A2Task):
        return asyncio.run(self._agent.handle(task))
