"""Evaluation metrics (v5 section 18 "Evaluation and Experimentation Plan").

Computed from the Context Service's own records (incidents, decisions, actions
and the hash-linked audit trail), so they measure what actually happened:

* MTTA  incident opened -> action selected (policy decision recorded)
* MTTR  incident opened -> verified recovery (incident RESOLVED)
* HITL rate, autonomous resolution rate
* action success rate, false action rate (executed but not verified)
* policy AUTO / APPROVAL_REQUIRED / BLOCK distribution
* message reliability (dead-lettered / published) and audit-chain validity

MTTD needs the fault-injection time; the experiment harness supplies it per
scenario (`detection_delay`).
"""

from collections import Counter
from datetime import datetime
from statistics import mean
from typing import Any

from aiops_platform.context.service import Principal

ORCH = Principal.orchestrator()


def _first(trail: list[dict[str, Any]], predicate) -> datetime | None:
    for row in trail:
        if predicate(row):
            return row["created_at"]
    return None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def _avg(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(mean(known), 3) if known else None


async def evaluate_workflows(platform) -> dict[str, Any]:
    incidents = await platform.context.list_incidents(ORCH)
    rows = []
    policy = Counter()
    for ctx in incidents:
        trail = await platform.context.audit_trail(ORCH, ctx.incident_id)
        opened = ctx.opened_at
        decided = _first(trail, lambda r: r["action"] == "DECISION_RECORDED" and r["detail_json"].get("stage") == "POLICY")
        resolved = _first(trail, lambda r: r["action"] == "INCIDENT_UPDATED" and r["detail_json"].get("status") == "RESOLVED")
        approval_wait = _seconds(
            _first(trail, lambda r: r["action"] == "APPROVAL_WAITING_FOR_HUMAN"),
            _first(trail, lambda r: r["action"] in ("APPROVAL_APPROVED", "APPROVAL_REJECTED", "APPROVAL_EXPIRED")),
        )
        for row in trail:
            if row["action"] == "DECISION_RECORDED" and row["detail_json"].get("stage") == "POLICY":
                policy[row["detail_json"]["type"]] += 1
        executed = [a for a in ctx.candidate_actions if a.execution_status not in ("CANDIDATE", "PLAN_REJECTED", "REJECTED_BY_HUMAN")]
        human = any(r["action"] == "APPROVAL_WAITING_FOR_HUMAN" for r in trail)
        rows.append(
            {
                "incident_id": ctx.incident_id,
                "kind": ctx.kind,
                "status": ctx.status,
                "hitl": human,
                "mtta_s": _seconds(opened, decided),
                "mttr_s": _seconds(opened, resolved),
                "approval_wait_s": approval_wait,
                "executed": len(executed),
                "verified": sum(a.execution_status == "VERIFIED" for a in executed),
                "audit_chain_valid": await platform.context.verify_audit_chain(ORCH, ctx.incident_id),
            }
        )
    incident_rows = [r for r in rows if r["kind"] == "INCIDENT"]
    resolved = [r for r in incident_rows if r["status"] == "RESOLVED"]
    executed = sum(r["executed"] for r in rows)
    verified = sum(r["verified"] for r in rows)
    published = len(platform.bus.published)
    dead = sum(1 for r in platform.bus.published if r.topic.endswith(".dlq"))
    return {
        "workflows": len(rows),
        "incidents": len(incident_rows),
        "mtta_s_avg": _avg([r["mtta_s"] for r in incident_rows]),
        "mttr_s_avg": _avg([r["mttr_s"] for r in resolved]),
        "hitl_rate": round(sum(r["hitl"] for r in rows) / len(rows), 3) if rows else None,
        "autonomous_resolution_rate": round(sum(not r["hitl"] for r in resolved) / len(incident_rows), 3) if incident_rows else None,
        "action_success_rate": round(verified / executed, 3) if executed else None,
        "false_action_rate": round((executed - verified) / executed, 3) if executed else None,
        "policy_distribution": dict(policy),
        "message_reliability": round(1 - dead / published, 4) if published else None,
        "audit_chains_valid": all(r["audit_chain_valid"] for r in rows),
        "per_workflow": rows,
    }
