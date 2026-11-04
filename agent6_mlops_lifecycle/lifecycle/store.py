"""Scoped durable jobs, immutable training evidence, review decisions and deployment outbox."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent6_mlops_lifecycle.assurance.store import AssuranceStore, encode
from agent6_mlops_lifecycle.monitoring.models import MonitorScope

ACTIVE = {"QUEUED", "TRAINING", "AWAITING_REVIEW", "DEPLOYMENT_READY"}


class LifecycleStore:
    def __init__(self, path: Path, scope: MonitorScope) -> None:
        self.evidence = AssuranceStore(path, scope)

    def schedule(
        self, version: str, report_id: str, policy: dict[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        now = now or datetime.now(UTC)
        identity = sha256(encode({"model": version, "report": report_id})).hexdigest()
        with self.evidence.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM jobs WHERE id=?", (identity,)).fetchone():
                return None
            recent = connection.execute(
                "SELECT payload,digest FROM jobs WHERE kind='lifecycle' ORDER BY updated DESC"
            ).fetchall()
            parsed = []
            for raw, digest in recent:
                if sha256(raw).hexdigest() != digest:
                    raise ValueError("Lifecycle checkpoint failed integrity validation")
                parsed.append(json.loads(raw))
            if (
                sum(
                    datetime.fromisoformat(job["created_at"]).date() == now.date() for job in parsed
                )
                >= policy["max_jobs_per_day"]
            ):
                return None
            for job in parsed:
                if job["model_version"] == version and (
                    job["status"] in ACTIVE
                    or (now - datetime.fromisoformat(job["created_at"]))
                    < timedelta(hours=policy["cooldown_hours"])
                ):
                    return None
            value = {
                "job_id": identity,
                "model_version": version,
                "source_report_id": report_id,
                "status": "QUEUED",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "policy": policy,
            }
            raw = encode(value)
            connection.execute(
                "INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?)",
                (identity, "lifecycle", now.timestamp(), raw, sha256(raw).hexdigest()),
            )
        return value

    def save(self, job: dict[str, Any], **changes: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        value = {**job, **changes, "updated_at": now.isoformat()}
        raw = encode(value)
        with self.evidence.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,digest FROM jobs WHERE id=? AND kind='lifecycle'",
                (job["job_id"],),
            ).fetchone()
            if row is None or sha256(row[0]).hexdigest() != row[1]:
                raise ValueError("Lifecycle job is missing or its integrity differs")
            if row[0] != encode(job):
                raise ValueError("Lifecycle state changed; reload before updating")
            connection.execute(
                "UPDATE jobs SET payload=?,digest=?,updated=? WHERE id=?",
                (raw, sha256(raw).hexdigest(), now.timestamp(), job["job_id"]),
            )
        return value

    def jobs(self) -> list[dict[str, Any]]:
        with self.evidence.connect() as connection:
            keys = connection.execute(
                "SELECT id FROM jobs WHERE kind='lifecycle' ORDER BY updated DESC LIMIT 100"
            ).fetchall()
        return [job for (key,) in keys if (job := self.evidence.job(key)) is not None]

    def resolve(
        self, identity: str, decision: str, digest: str, actor: str, note: str
    ) -> dict[str, Any]:
        if decision not in ("APPROVE", "REJECT"):
            raise ValueError("Unknown lifecycle review decision")
        with self.evidence.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,digest FROM jobs WHERE id=? AND kind='lifecycle'", (identity,)
            ).fetchone()
            if row is None:
                raise LookupError("Lifecycle request not found")
            if sha256(row[0]).hexdigest() != row[1]:
                raise ValueError("Lifecycle request integrity differs")
            job: dict[str, Any] = json.loads(row[0])
            if job["status"] != "AWAITING_REVIEW" or digest != job.get("candidate_sha256"):
                raise ValueError("Review does not bind the pending exact candidate")
            now = datetime.now(UTC)
            if now > datetime.fromisoformat(job["review_expires_at"]):
                raise ValueError("Candidate review has expired")
            audit = {
                "job_id": identity,
                "decision": decision,
                "candidate_sha256": digest,
                "actor": actor,
                "note": note,
                "at": now.isoformat(),
            }
            audit_bytes = encode(audit)
            reference = sha256(audit_bytes).hexdigest()
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?)", (reference, "review", audit_bytes)
            )
            job.update(
                status="DEPLOYMENT_READY" if decision == "APPROVE" else "REJECTED",
                review_ref=reference,
                updated_at=now.isoformat(),
            )
            raw = encode(job)
            connection.execute(
                "UPDATE jobs SET payload=?,digest=?,updated=? WHERE id=?",
                (raw, sha256(raw).hexdigest(), now.timestamp(), identity),
            )
        return job
