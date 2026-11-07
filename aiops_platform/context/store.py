"""Authoritative operational state (v5 section 6).

`ContextStore` is the persistence layer only. Access rules (who may read, who
may append) are enforced one level up in `service.py`; nothing outside the
Context Service process should hold a `ContextStore`.

Every write runs in one transaction that also appends a hash-linked audit
record and bumps the incident `version`, so the audit trail and the
version an action was decided on are always consistent with the data.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from aiops_platform.context import schema as t
from aiops_platform.context.objects import ObjectStore, canonical_bytes
from aiops_platform.contracts.context import (
    CandidateAction,
    EvidenceReference,
    HitlSummary,
    IncidentContext,
    WorkflowKind,
)
from aiops_platform.contracts.enums import Environment, Priority, Severity
from aiops_platform.contracts.events import ServiceIdentity
from aiops_platform.contracts.tasks import AgentResult

GENESIS = "0" * 64


class IncidentNotFound(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; every stored timestamp is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _hash(prev: str, record: dict[str, Any]) -> str:
    material = prev + json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ContextStore:
    def __init__(self, engine: AsyncEngine, objects: ObjectStore) -> None:
        self._engine = engine
        self.objects = objects

    @classmethod
    async def connect(cls, url: str, objects: ObjectStore) -> "ContextStore":
        """`postgresql+psycopg://...` in compose, `sqlite+aiosqlite:///...` in tests."""
        engine = create_async_engine(url, future=True)
        store = cls(engine, objects)
        async with engine.begin() as conn:
            await conn.run_sync(t.metadata.create_all)
        return store

    async def close(self) -> None:
        await self._engine.dispose()

    # ---- incidents -------------------------------------------------------

    async def open_incident(
        self,
        *,
        incident_id: str,
        kind: WorkflowKind,
        service: ServiceIdentity,
        environment: Environment,
        severity: Severity,
        priority: Priority,
        signals: dict[str, JsonValue],
        actor: str,
    ) -> tuple[IncidentContext, bool]:
        """Open an incident, or return the existing one (idempotent). Second item: created."""
        now = _now()
        async with self._engine.begin() as conn:
            existing = await self._incident_row(conn, incident_id)
            if existing is not None:
                return await self._context(conn, incident_id), False
            await conn.execute(
                t.incidents.insert().values(
                    incident_id=incident_id,
                    kind=kind,
                    service_id=service.service_id,
                    namespace=service.namespace,
                    service_version=service.version,
                    environment=environment.value,
                    severity=severity.value,
                    priority=priority.value,
                    status="OPEN",
                    latest_signals=signals,
                    version=1,
                    opened_at=now,
                    updated_at=now,
                )
            )
            await self._audit(conn, incident_id, actor, "INCIDENT_OPENED", {"kind": kind})
            return await self._context(conn, incident_id), True

    async def update_incident(
        self,
        incident_id: str,
        *,
        actor: str,
        status: str | None = None,
        severity: Severity | None = None,
        priority: Priority | None = None,
        signals: dict[str, JsonValue] | None = None,
        closed: bool = False,
    ) -> IncidentContext:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if severity is not None:
            values["severity"] = severity.value
        if priority is not None:
            values["priority"] = priority.value
        if signals is not None:
            values["latest_signals"] = signals
        if closed:
            values["closed_at"] = _now()
        async with self._engine.begin() as conn:
            await self._require(conn, incident_id)
            if values:
                await conn.execute(
                    update(t.incidents).where(t.incidents.c.incident_id == incident_id).values(**values)
                )
            detail = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in values.items()}
            await self._audit(conn, incident_id, actor, "INCIDENT_UPDATED", detail)
            return await self._context(conn, incident_id)

    async def get_incident_context(self, incident_id: str) -> IncidentContext:
        async with self._engine.connect() as conn:
            await self._require(conn, incident_id)
            return await self._context(conn, incident_id)

    async def list_incidents(self, limit: int = 100) -> list[IncidentContext]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(t.incidents.c.incident_id)
                    .order_by(t.incidents.c.opened_at.desc())
                    .limit(limit)
                )
            ).all()
            return [await self._context(conn, r.incident_id) for r in rows]

    # ---- agent results and evidence (agent append-only) ------------------

    async def append_agent_result(self, result: AgentResult, result_type: str, *, actor: str) -> bool:
        """Store one immutable result; False when this task's result already exists."""
        async with self._engine.begin() as conn:
            await self._require(conn, result.incident_id)
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        t.agent_results.insert().values(
                            result_id=result.result_id,
                            incident_id=result.incident_id,
                            agent_id=result.agent_id.value,
                            task_id=result.task_id,
                            result_type=result_type,
                            status=result.status.value,
                            confidence=result.confidence,
                            result_json=result.model_dump(mode="json"),
                            created_at=_now(),
                        )
                    )
            except IntegrityError:
                return False
            await self._audit(
                conn,
                result.incident_id,
                actor,
                "AGENT_RESULT_APPENDED",
                {"agent_id": result.agent_id.value, "task_id": result.task_id, "type": result_type},
            )
            return True

    async def append_evidence(
        self,
        *,
        incident_id: str,
        source: str,
        evidence_type: str,
        summary: str,
        content: JsonValue,
        observed_at: datetime,
        actor: str,
    ) -> EvidenceReference:
        data = canonical_bytes(content)
        object_uri, digest = self.objects.put(incident_id, data)
        async with self._engine.begin() as conn:
            await self._require(conn, incident_id)
            existing = (
                await conn.execute(
                    select(t.evidence_metadata).where(
                        t.evidence_metadata.c.incident_id == incident_id,
                        t.evidence_metadata.c.checksum_sha256 == digest,
                    )
                )
            ).first()
            if existing is not None:
                return self._evidence_ref(existing)
            evidence_id = f"EVD-{evidence_type}-{digest[:12]}"
            await conn.execute(
                t.evidence_metadata.insert().values(
                    evidence_id=evidence_id,
                    incident_id=incident_id,
                    source=source,
                    evidence_type=evidence_type,
                    summary=summary,
                    checksum_sha256=digest,
                    object_uri=object_uri,
                    observed_at=observed_at,
                    created_at=_now(),
                )
            )
            await self._audit(
                conn, incident_id, actor, "EVIDENCE_APPENDED", {"evidence_id": evidence_id}
            )
            row = (
                await conn.execute(
                    select(t.evidence_metadata).where(t.evidence_metadata.c.evidence_id == evidence_id)
                )
            ).one()
            return self._evidence_ref(row)

    async def get_latest_agent_results(
        self, incident_id: str, agent_ids: Sequence[str] = ()
    ) -> tuple[AgentResult, ...]:
        """Latest result per agent, oldest first."""
        async with self._engine.connect() as conn:
            await self._require(conn, incident_id)
            results = await self._results(conn, incident_id)
        latest: dict[str, AgentResult] = {}
        for result in results:
            latest[result.agent_id.value] = result
        wanted = set(agent_ids)
        return tuple(r for a, r in latest.items() if not wanted or a in wanted)

    async def read_evidence(self, evidence_id: str) -> tuple[EvidenceReference, JsonValue]:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(t.evidence_metadata).where(t.evidence_metadata.c.evidence_id == evidence_id)
                )
            ).first()
        if row is None:
            raise KeyError(evidence_id)
        return self._evidence_ref(row), json.loads(self.objects.get(row.object_uri))

    # ---- orchestrator writes --------------------------------------------

    async def record_action(self, incident_id: str, action: CandidateAction, *, actor: str) -> None:
        now = _now()
        async with self._engine.begin() as conn:
            await self._require(conn, incident_id)
            key = (t.actions.c.action_id == action.action_id) & (
                t.actions.c.action_version == action.action_version
            )
            exists = (await conn.execute(select(t.actions.c.action_id).where(key))).first()
            if exists is None:
                await conn.execute(
                    t.actions.insert().values(
                        action_id=action.action_id,
                        action_version=action.action_version,
                        incident_id=incident_id,
                        action_type=action.type,
                        source_agent=action.source_agent,
                        score=action.score,
                        parameters_json=action.parameters,
                        policy_result=action.policy_result.value if action.policy_result else None,
                        execution_status=action.execution_status,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await self._audit(
                    conn,
                    incident_id,
                    actor,
                    "ACTION_RECORDED",
                    {"action_id": action.action_id, "type": action.type, "score": action.score},
                )

    async def update_action(
        self,
        incident_id: str,
        action_id: str,
        action_version: int,
        *,
        actor: str,
        policy_result: str | None = None,
        risk: dict[str, JsonValue] | None = None,
        execution_status: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"updated_at": _now()}
        if policy_result is not None:
            values["policy_result"] = policy_result
        if risk is not None:
            values["risk_json"] = risk
        if execution_status is not None:
            values["execution_status"] = execution_status
        async with self._engine.begin() as conn:
            await self._require(conn, incident_id)
            await conn.execute(
                update(t.actions)
                .where(
                    (t.actions.c.action_id == action_id)
                    & (t.actions.c.action_version == action_version)
                )
                .values(**values)
            )
            detail = {k: v for k, v in values.items() if k != "updated_at"}
            await self._audit(conn, incident_id, actor, "ACTION_UPDATED", {"action_id": action_id, **detail})

    async def record_decision(
        self,
        incident_id: str,
        *,
        stage: str,
        decision_type: str,
        payload: dict[str, JsonValue],
        actor: str,
    ) -> str:
        decision_id = f"DEC-{uuid4().hex[:12]}"
        async with self._engine.begin() as conn:
            await self._require(conn, incident_id)
            await conn.execute(
                t.decisions.insert().values(
                    decision_id=decision_id,
                    incident_id=incident_id,
                    stage=stage,
                    decision_type=decision_type,
                    payload_json=payload,
                    created_at=_now(),
                )
            )
            await self._audit(
                conn,
                incident_id,
                actor,
                "DECISION_RECORDED",
                {"decision_id": decision_id, "stage": stage, "type": decision_type, **payload},
            )
        return decision_id

    async def record_model_event(
        self,
        incident_id: str,
        *,
        model_name: str,
        model_version: str,
        event_type: str,
        status: str,
        metrics: dict[str, JsonValue],
        actor: str,
    ) -> str:
        event_id = f"MEV-{uuid4().hex[:12]}"
        async with self._engine.begin() as conn:
            await self._require(conn, incident_id)
            await conn.execute(
                t.model_events.insert().values(
                    event_id=event_id,
                    incident_id=incident_id,
                    model_name=model_name,
                    model_version=model_version,
                    event_type=event_type,
                    status=status,
                    metrics_json=metrics,
                    created_at=_now(),
                )
            )
            await self._audit(
                conn,
                incident_id,
                actor,
                "MODEL_EVENT_RECORDED",
                {"event_id": event_id, "model": f"{model_name}:{model_version}", "type": event_type},
            )
        return event_id

    # ---- approvals (written by the HITL Service) -------------------------

    async def upsert_approval(self, row: dict[str, Any], *, actor: str) -> None:
        async with self._engine.begin() as conn:
            await self._require(conn, row["incident_id"])
            key = t.approvals.c.approval_id == row["approval_id"]
            exists = (await conn.execute(select(t.approvals.c.approval_id).where(key))).first()
            if exists is None:
                await conn.execute(t.approvals.insert().values(**row))
            else:
                await conn.execute(update(t.approvals).where(key).values(**row))
            await self._audit(
                conn,
                row["incident_id"],
                actor,
                f"APPROVAL_{row['state']}",
                {
                    "approval_id": row["approval_id"],
                    "action_id": row["action_id"],
                    "decision": row.get("decision"),
                    "approver_id": row.get("approver_id"),
                },
            )

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(t.approvals).where(t.approvals.c.approval_id == approval_id))
            ).first()
        if row is None:
            return None
        data = dict(row._mapping)
        for key in ("created_at", "expires_at", "resolved_at"):
            data[key] = _aware(data[key])
        return data

    async def list_approvals(self, *, state: str | None = None) -> list[dict[str, Any]]:
        query = select(t.approvals).order_by(t.approvals.c.created_at.desc())
        if state is not None:
            query = query.where(t.approvals.c.state == state)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        out = []
        for row in rows:
            data = dict(row._mapping)
            for key in ("created_at", "expires_at", "resolved_at"):
                data[key] = _aware(data[key])
            out.append(data)
        return out

    # ---- audit -----------------------------------------------------------

    async def audit_trail(self, incident_id: str) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(t.audit_log)
                    .where(t.audit_log.c.incident_id == incident_id)
                    .order_by(t.audit_log.c.seq)
                )
            ).all()
        return [
            {**dict(r._mapping), "created_at": _aware(r.created_at)} for r in rows
        ]

    async def verify_audit_chain(self, incident_id: str) -> bool:
        prev = GENESIS
        for row in await self.audit_trail(incident_id):
            record = {
                "incident_id": row["incident_id"],
                "actor": row["actor"],
                "action": row["action"],
                "detail": row["detail_json"],
                "created_at": row["created_at"].isoformat(),
            }
            if row["prev_hash"] != prev or row["hash"] != _hash(prev, record):
                return False
            prev = row["hash"]
        return True

    # ---- internals -------------------------------------------------------

    async def _incident_row(self, conn: AsyncConnection, incident_id: str):
        return (
            await conn.execute(select(t.incidents).where(t.incidents.c.incident_id == incident_id))
        ).first()

    async def _require(self, conn: AsyncConnection, incident_id: str) -> None:
        if await self._incident_row(conn, incident_id) is None:
            raise IncidentNotFound(incident_id)

    async def _audit(
        self, conn: AsyncConnection, incident_id: str, actor: str, action: str, detail: dict[str, Any]
    ) -> None:
        last = (
            await conn.execute(
                select(t.audit_log.c.hash)
                .where(t.audit_log.c.incident_id == incident_id)
                .order_by(t.audit_log.c.seq.desc())
                .limit(1)
            )
        ).first()
        prev = last.hash if last else GENESIS
        now = _now()
        detail = json.loads(json.dumps(detail, default=str))
        record = {
            "incident_id": incident_id,
            "actor": actor,
            "action": action,
            "detail": detail,
            "created_at": now.isoformat(),
        }
        await conn.execute(
            t.audit_log.insert().values(
                incident_id=incident_id,
                actor=actor,
                action=action,
                detail_json=detail,
                created_at=now,
                prev_hash=prev,
                hash=_hash(prev, record),
            )
        )
        await conn.execute(
            update(t.incidents)
            .where(t.incidents.c.incident_id == incident_id)
            .values(version=t.incidents.c.version + 1, updated_at=now)
        )

    async def _results(self, conn: AsyncConnection, incident_id: str) -> list[AgentResult]:
        rows = (
            await conn.execute(
                select(t.agent_results.c.result_json)
                .where(t.agent_results.c.incident_id == incident_id)
                .order_by(t.agent_results.c.created_at)
            )
        ).all()
        return [AgentResult.model_validate(r.result_json) for r in rows]

    @staticmethod
    def _evidence_ref(row) -> EvidenceReference:
        return EvidenceReference(
            evidence_id=row.evidence_id,
            incident_id=row.incident_id,
            source=row.source,
            object_uri=row.object_uri,
            checksum_sha256=row.checksum_sha256,
            observed_at=_aware(row.observed_at),
        )

    async def _context(self, conn: AsyncConnection, incident_id: str) -> IncidentContext:
        inc = await self._incident_row(conn, incident_id)
        results = await self._results(conn, incident_id)
        evidence = (
            await conn.execute(
                select(t.evidence_metadata)
                .where(t.evidence_metadata.c.incident_id == incident_id)
                .order_by(t.evidence_metadata.c.created_at)
            )
        ).all()
        action_rows = (
            await conn.execute(
                select(t.actions)
                .where(t.actions.c.incident_id == incident_id)
                .order_by(t.actions.c.score.desc())
            )
        ).all()
        approval = (
            await conn.execute(
                select(t.approvals.c.approval_id, t.approvals.c.state)
                .where(t.approvals.c.incident_id == incident_id)
                .order_by(t.approvals.c.created_at.desc())
                .limit(1)
            )
        ).first()
        return IncidentContext(
            incident_id=inc.incident_id,
            version=inc.version,
            environment=Environment(inc.environment),
            status=inc.status,
            agent_results=tuple(results),
            evidence_refs=tuple(self._evidence_ref(e) for e in evidence),
            kind=inc.kind,
            priority=Priority(inc.priority),
            severity=Severity(inc.severity),
            service=ServiceIdentity(
                service_id=inc.service_id, namespace=inc.namespace, version=inc.service_version
            ),
            latest_signals=inc.latest_signals or {},
            candidate_actions=tuple(
                CandidateAction(
                    action_id=a.action_id,
                    action_version=a.action_version,
                    type=a.action_type,
                    source_agent=a.source_agent,
                    score=a.score,
                    parameters=a.parameters_json or {},
                    policy_result=a.policy_result,
                    execution_status=a.execution_status,
                )
                for a in action_rows
            ),
            hitl=HitlSummary(state=approval.state, approval_id=approval.approval_id)
            if approval
            else HitlSummary(),
            opened_at=_aware(inc.opened_at),
            updated_at=_aware(inc.updated_at),
            closed_at=_aware(inc.closed_at),
        )


async def count_rows(engine: AsyncEngine, table) -> int:  # test helper
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()
