"""Local durable project catalog and immutable, digest-verified batch storage."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_projects import EvaluationBatch, Name, ProjectManifest
from agent6_mlops_lifecycle.contracts.mlops_storage import (
    StoredEvaluationReference,
    project_policy_fingerprint,
)
from agent6_mlops_lifecycle.interfaces.project_storage import (
    EvidenceIntegrityError,
    EvidenceNotFoundError,
    ProjectStorageConflictError,
    ProjectStorageUnavailableError,
)

MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
_APPLICATION_ID = 0x41364D4C


class _ManifestDocument(Contract):
    schema_version: Literal["1"] = "1"
    tenant_id: Name
    manifest: ProjectManifest


class _BatchDocument(Contract):
    schema_version: Literal["1"] = "1"
    tenant_id: Name
    policy_version: Name
    manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    window_start: AwareDatetime
    window_end: AwareDatetime
    source_batch: EvaluationBatch

    @model_validator(mode="after")
    def check_samples(self) -> "_BatchDocument":
        if self.window_start >= self.window_end:
            raise ValueError("Evidence window start must precede its end")
        if any(
            not self.window_start <= sample.observed_at < self.window_end
            for sample in self.source_batch.samples
        ):
            raise ValueError("Evaluation observations must belong to the declared time window")
        context = self.source_batch.evidence_context
        if context is not None and (
            context.window_start != self.window_start or context.window_end != self.window_end
        ):
            raise ValueError("Batch evidence context differs from the stored window")
        return self


def _canonical(document: Contract) -> bytes:
    return json.dumps(
        document.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _reference(document: _BatchDocument, digest: str) -> StoredEvaluationReference:
    batch = document.source_batch
    return StoredEvaluationReference(
        tenant_id=document.tenant_id,
        project_id=batch.project_id,
        model_id=batch.model_id,
        model_version=batch.model_version,
        policy_version=document.policy_version,
        policy_fingerprint=document.manifest_sha256,
        dataset_ref=batch.dataset_ref,
        window_start=document.window_start,
        window_end=document.window_end,
        evidence_ref=f"sha256:{digest}",
    )


class SQLiteProjectStore:
    """Trusted local adapter; tenant scope is not user authentication.

    One file holds immutable manifests and bounded JSON batches. Callers supply
    tenant identity from trusted application configuration, never an unauthenticated
    task. PostgreSQL can implement the same interfaces for a deployed service.
    """

    def __init__(self, path: Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("A file database is required for durable project storage")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if (app_id == 0 and (version != 0 or tables)) or app_id not in (0, _APPLICATION_ID):
                raise ProjectStorageUnavailableError("Database belongs to a different application")
            if version not in (0, 1):
                raise ProjectStorageUnavailableError("Unsupported Agent 6 database schema version")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS project_manifests (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    document BLOB NOT NULL,
                    PRIMARY KEY (tenant_id, project_id, model_id, model_version)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS evaluation_batches (
                    evidence_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    dataset_ref TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    document BLOB NOT NULL,
                    FOREIGN KEY (tenant_id, project_id, model_id, model_version)
                        REFERENCES project_manifests
                        (tenant_id, project_id, model_id, model_version)
                )"""
            )
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                yield connection
        except sqlite3.Error as error:
            raise ProjectStorageUnavailableError("Agent 6 storage operation failed") from error

    @staticmethod
    def _manifest(
        connection: sqlite3.Connection, scope: tuple[str, str, str, str]
    ) -> ProjectManifest | None:
        row = connection.execute(
            """SELECT policy_version, digest, length(document) AS size
               FROM project_manifests
               WHERE tenant_id = ? AND project_id = ? AND model_id = ? AND model_version = ?""",
            scope,
        ).fetchone()
        if row is None:
            return None
        if not 0 < row["size"] <= MAX_MANIFEST_BYTES:
            raise EvidenceIntegrityError("Stored manifest exceeds its permitted size")
        stored = connection.execute(
            """SELECT document FROM project_manifests
               WHERE tenant_id = ? AND project_id = ? AND model_id = ? AND model_version = ?""",
            scope,
        ).fetchone()
        payload = stored["document"]
        if not isinstance(payload, bytes) or sha256(payload).hexdigest() != row["digest"]:
            raise EvidenceIntegrityError("Stored manifest digest does not match its contents")
        try:
            document = _ManifestDocument.model_validate_json(payload)
        except ValueError as error:
            raise EvidenceIntegrityError("Stored manifest schema is invalid") from error
        manifest = document.manifest
        actual = (
            document.tenant_id,
            manifest.project_id,
            manifest.model_id,
            manifest.model_version,
        )
        if actual != scope or manifest.policy_version != row["policy_version"]:
            raise EvidenceIntegrityError("Stored manifest identity differs from its metadata")
        return manifest

    def register_manifest(self, tenant_id: str, manifest: ProjectManifest) -> None:
        document = _ManifestDocument(tenant_id=tenant_id, manifest=manifest)
        # Revalidate values even when callers used model_copy or model_construct.
        document = _ManifestDocument.model_validate_json(document.model_dump_json())
        payload = _canonical(document)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("Project manifest exceeds its permitted size")
        value = document.manifest
        scope = (tenant_id, value.project_id, value.model_id, value.model_version)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._manifest(connection, scope)
            if previous is not None:
                if previous != value:
                    raise ProjectStorageConflictError(
                        "A registered model version cannot silently change policy"
                    )
                return
            connection.execute(
                "INSERT INTO project_manifests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*scope, value.policy_version, sha256(payload).hexdigest(), payload),
            )

    def get_manifest(
        self, tenant_id: str, project_id: str, model_id: str, model_version: str
    ) -> ProjectManifest | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._manifest(connection, (tenant_id, project_id, model_id, model_version))

    def save_batch(
        self,
        tenant_id: str,
        manifest: ProjectManifest,
        batch: EvaluationBatch,
        window_start: datetime,
        window_end: datetime,
    ) -> StoredEvaluationReference:
        value = EvaluationBatch.model_validate_json(batch.model_dump_json())
        model_scope = (tenant_id, value.project_id, value.model_id, value.model_version)
        if model_scope[1:] != (manifest.project_id, manifest.model_id, manifest.model_version):
            raise ValueError("Batch does not match the registered project and model")
        if any(sample.task_type != manifest.task_type for sample in value.samples):
            raise ValueError("Evaluation samples do not match the registered project task type")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            registered = self._manifest(connection, model_scope)
            if registered is None or registered != manifest:
                raise ValueError("Exact project manifest must be registered before saving evidence")
            manifest_digest = project_policy_fingerprint(registered)
            document = _BatchDocument(
                tenant_id=tenant_id,
                policy_version=registered.policy_version,
                manifest_sha256=manifest_digest,
                window_start=window_start,
                window_end=window_end,
                source_batch=value,
            )
            document = document.model_copy(
                update={
                    "window_start": document.window_start.astimezone(UTC),
                    "window_end": document.window_end.astimezone(UTC),
                }
            )
            payload = _canonical(document)
            if len(payload) > MAX_BATCH_BYTES:
                raise ValueError("Evaluation evidence exceeds its permitted size")
            digest = sha256(payload).hexdigest()
            reference = _reference(document, digest)
            previous = connection.execute(
                "SELECT document FROM evaluation_batches WHERE evidence_ref = ?",
                (reference.evidence_ref,),
            ).fetchone()
            if previous is not None:
                if previous["document"] != payload:
                    raise EvidenceIntegrityError(
                        "Evidence identity was reused with changed contents"
                    )
                # Verify metadata too; idempotent imports must not accept corrupted rows.
                self._load(connection, reference)
                return reference
            connection.execute(
                "INSERT INTO evaluation_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reference.evidence_ref,
                    *model_scope,
                    reference.policy_version,
                    reference.dataset_ref,
                    _utc(reference.window_start),
                    _utc(reference.window_end),
                    payload,
                ),
            )
            return reference

    @staticmethod
    def _load(
        connection: sqlite3.Connection, reference: StoredEvaluationReference
    ) -> EvaluationBatch:
        parameters = (
            reference.evidence_ref,
            reference.tenant_id,
            reference.project_id,
            reference.model_id,
            reference.model_version,
            reference.policy_version,
            reference.dataset_ref,
            _utc(reference.window_start),
            _utc(reference.window_end),
        )
        condition = """evidence_ref = ? AND tenant_id = ? AND project_id = ?
            AND model_id = ? AND model_version = ? AND policy_version = ?
            AND dataset_ref = ? AND window_start = ? AND window_end = ?"""
        row = connection.execute(
            f"SELECT length(document) AS size FROM evaluation_batches WHERE {condition}",
            parameters,
        ).fetchone()
        if row is None:
            raise EvidenceNotFoundError("Evidence is missing or outside the requested scope")
        if not 0 < row["size"] <= MAX_BATCH_BYTES:
            raise EvidenceIntegrityError("Stored evaluation evidence exceeds its permitted size")
        stored = connection.execute(
            f"SELECT document FROM evaluation_batches WHERE {condition}", parameters
        ).fetchone()
        payload = stored["document"]
        if (
            not isinstance(payload, bytes)
            or f"sha256:{sha256(payload).hexdigest()}" != reference.evidence_ref
        ):
            raise EvidenceIntegrityError("Stored evidence digest does not match its contents")
        try:
            document = _BatchDocument.model_validate_json(payload)
        except ValueError as error:
            raise EvidenceIntegrityError("Stored evaluation evidence schema is invalid") from error
        if _reference(document, sha256(payload).hexdigest()) != reference:
            raise EvidenceIntegrityError(
                "Stored evidence identity differs from the requested scope"
            )
        manifest = SQLiteProjectStore._manifest(
            connection,
            (
                reference.tenant_id,
                reference.project_id,
                reference.model_id,
                reference.model_version,
            ),
        )
        if manifest is None or project_policy_fingerprint(manifest) != document.manifest_sha256:
            raise EvidenceIntegrityError("Evidence is bound to a different project policy")
        if any(sample.task_type != manifest.task_type for sample in document.source_batch.samples):
            raise EvidenceIntegrityError("Stored samples do not match the registered task type")
        # source_batch.evidence_ref is import metadata. Reports use the verified store digest.
        return EvaluationBatch.model_validate(
            {
                **document.source_batch.model_dump(mode="json"),
                "evidence_ref": reference.evidence_ref,
            }
        )

    def load_batch(self, reference: StoredEvaluationReference) -> EvaluationBatch:
        value = StoredEvaluationReference.model_validate_json(reference.model_dump_json())
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._load(connection, value)
