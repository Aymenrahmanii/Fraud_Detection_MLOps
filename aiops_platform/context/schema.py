"""PostgreSQL tables of the Context Service (v5 section 6.2).

Defined with SQLAlchemy Core so the same schema runs on PostgreSQL (compose)
and SQLite (tests). JSON columns hold the typed contract payloads.
"""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

incidents = Table(
    "incidents",
    metadata,
    Column("incident_id", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("service_id", String(128), nullable=False),
    Column("namespace", String(128), nullable=False),
    Column("service_version", String(64), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("priority", String(4), nullable=False),
    Column("status", String(32), nullable=False),
    Column("latest_signals", JSON, nullable=False, default=dict),
    # Incremented by every write; Agent 4 preconditions bind to it (context_version==N).
    Column("version", Integer, nullable=False, default=1),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
)

agent_results = Table(
    "agent_results",
    metadata,
    Column("result_id", String(128), primary_key=True),
    Column("incident_id", String(64), ForeignKey("incidents.incident_id"), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("task_id", String(128), nullable=False),
    Column("result_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("confidence", Float),
    Column("result_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # One immutable result per agent execution: a redelivered append is a no-op.
    UniqueConstraint("incident_id", "agent_id", "task_id", name="uq_agent_result_task"),
)

actions = Table(
    "actions",
    metadata,
    Column("action_id", String(128), primary_key=True),
    Column("action_version", Integer, primary_key=True),
    Column("incident_id", String(64), ForeignKey("incidents.incident_id"), nullable=False),
    Column("action_type", String(32), nullable=False),
    Column("source_agent", String(64), nullable=False),
    Column("score", Float, nullable=False),
    Column("parameters_json", JSON, nullable=False),
    Column("risk_json", JSON),
    Column("policy_result", String(32)),
    Column("execution_status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

approvals = Table(
    "approvals",
    metadata,
    Column("approval_id", String(128), primary_key=True),
    Column("incident_id", String(64), ForeignKey("incidents.incident_id"), nullable=False),
    Column("action_id", String(128), nullable=False),
    Column("action_version", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("decision", String(16)),
    Column("approver_id", String(128)),
    Column("reason", Text),
    Column("token_hash", String(64)),
    Column("request_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
)

model_events = Table(
    "model_events",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column("incident_id", String(64), ForeignKey("incidents.incident_id"), nullable=False),
    Column("model_name", String(128), nullable=False),
    Column("model_version", String(64), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("metrics_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

evidence_metadata = Table(
    "evidence_metadata",
    metadata,
    Column("evidence_id", String(128), primary_key=True),
    Column("incident_id", String(64), ForeignKey("incidents.incident_id"), nullable=False),
    Column("source", String(64), nullable=False),
    Column("evidence_type", String(64), nullable=False),
    Column("summary", Text, nullable=False),
    Column("checksum_sha256", String(64), nullable=False),
    Column("object_uri", Text, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Deduplicate identical evidence within one incident.
    UniqueConstraint("incident_id", "checksum_sha256", name="uq_evidence_checksum"),
)

decisions = Table(
    "decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column("incident_id", String(64), ForeignKey("incidents.incident_id"), nullable=False),
    Column("stage", String(64), nullable=False),
    Column("decision_type", String(64), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Hash-linked audit trail: every write appends (actor, action, detail) here.
audit_log = Table(
    "audit_log",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", String(64), nullable=False, index=True),
    Column("actor", String(64), nullable=False),
    Column("action", String(64), nullable=False),
    Column("detail_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)
