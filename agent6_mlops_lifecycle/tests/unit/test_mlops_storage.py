"""Durability, immutable policy and scoped evidence checks for Agent 6 storage."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

import agent6_mlops_lifecycle.adapters.sqlite_projects as storage_module
from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.contracts.enums import AgentId, Environment, ResultStatus
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    EvaluationBatch,
    ProjectManifest,
    TextEvaluationSample,
    TextJudgment,
)
from agent6_mlops_lifecycle.contracts.mlops_storage import StoredEvaluationReference
from agent6_mlops_lifecycle.contracts.tasks import AgentTask, TaskContext
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService
from agent6_mlops_lifecycle.interfaces.project_storage import (
    EvidenceIntegrityError,
    EvidenceNotFoundError,
    ProjectStorageConflictError,
    ProjectStorageUnavailableError,
)
from agent6_mlops_lifecycle.service import MLOpsLifecycleAgent

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 30, tzinfo=UTC)
END = START + timedelta(days=1)


def example(name: str = "support-routing") -> tuple[ProjectManifest, EvaluationBatch]:
    base = ROOT / "contracts/examples/agent6/projects"
    return (
        ProjectManifest.model_validate_json(
            (base / f"{name}-manifest.json").read_text(encoding="utf-8")
        ),
        EvaluationBatch.model_validate_json(
            (base / f"{name}-batch.json").read_text(encoding="utf-8")
        ),
    )


def prepared(tmp_path: Path) -> tuple[SQLiteProjectStore, StoredEvaluationReference]:
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    manifest, batch = example()
    store.register_manifest("tenant-a", manifest)
    return store, store.save_batch("tenant-a", manifest, batch, START, END)


def task(reference: StoredEvaluationReference, *, include_evidence: bool = True) -> AgentTask:
    return AgentTask(
        task_id="task-1",
        incident_id="incident-1",
        agent_id=AgentId.MLOPS_LIFECYCLE,
        objective="Evaluate persisted evidence",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        task_context=TaskContext(
            service_id="test",
            namespace="local",
            environment=Environment.TEST,
            signals={"project_evaluation_ref": reference.model_dump(mode="json")},
            evidence_refs=(reference.evidence_ref,) if include_evidence else (),
        ),
    )


def test_two_projects_survive_reopen_and_keep_verified_evidence(tmp_path: Path) -> None:
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    service = ProjectEvaluationService(manifests=store, batches=store, tenant_id="tenant-a")
    references = []
    for name in ("support-routing", "energy-demand"):
        manifest, batch = example(name)
        service.register_project(manifest)
        reference = service.store_batch(batch, START, END)
        assert reference.evidence_ref != batch.evidence_ref
        references.append(reference)
    reopened = SQLiteProjectStore(store.path)
    after_restart = ProjectEvaluationService(
        manifests=reopened, batches=reopened, tenant_id="tenant-a"
    )
    reports = [asyncio.run(after_restart.evaluate_reference(ref)) for ref in references]
    assert reports[0].metrics["f1"] == 1.0
    assert reports[1].metrics["mae"] == pytest.approx(0.1)
    assert all(report.decision == "PASS" for report in reports)
    assert [report.evidence_ref for report in reports] == [ref.evidence_ref for ref in references]
    assert all(report.requires_human_review for report in reports)


def test_manifest_and_batch_registration_are_idempotent_and_policy_is_immutable(
    tmp_path: Path,
) -> None:
    store, reference = prepared(tmp_path)
    manifest, batch = example()
    store.register_manifest("tenant-a", manifest)
    assert store.save_batch("tenant-a", manifest, batch, START, END) == reference
    changed = manifest.model_copy(update={"min_f1": 0.1})
    with pytest.raises(ProjectStorageConflictError, match="silently change"):
        SQLiteProjectStore(store.path).register_manifest("tenant-a", changed)
    store.register_manifest("tenant-b", changed)
    assert store.get_manifest("tenant-b", changed.project_id, changed.model_id, "v1") == changed


def test_concurrent_imports_keep_one_immutable_record(tmp_path: Path) -> None:
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    manifest, batch = example()

    def ingest(_index: int) -> StoredEvaluationReference:
        store.register_manifest("tenant-a", manifest)
        return store.save_batch("tenant-a", manifest, batch, START, END)

    with ThreadPoolExecutor(max_workers=4) as pool:
        references = list(pool.map(ingest, range(8)))
    assert all(reference == references[0] for reference in references)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "tenant-b"),
        ("project_id", "other-project"),
        ("model_id", "other-model"),
        ("model_version", "v2"),
        ("policy_version", "other-policy"),
        ("dataset_ref", "other-dataset"),
        ("window_start", START - timedelta(hours=1)),
        ("window_end", END + timedelta(hours=1)),
        ("evidence_ref", "sha256:" + "0" * 64),
    ],
)
def test_reference_cannot_access_wrong_scope_or_window(
    tmp_path: Path, field: str, value: object
) -> None:
    store, reference = prepared(tmp_path)
    with pytest.raises(EvidenceNotFoundError, match="scope"):
        store.load_batch(reference.model_copy(update={field: value}))


def test_changed_evidence_bytes_fail_digest_check(tmp_path: Path) -> None:
    store, reference = prepared(tmp_path)
    with sqlite3.connect(store.path) as connection:
        payload = connection.execute("SELECT document FROM evaluation_batches").fetchone()[0]
        content = json.loads(payload)
        content["source_batch"]["samples"][0]["predicted"] = 0
        connection.execute(
            "UPDATE evaluation_batches SET document = ?",
            (json.dumps(content).encode(),),
        )
    with pytest.raises(EvidenceIntegrityError, match="digest"):
        store.load_batch(reference)


@pytest.mark.parametrize("corruption", ["schema", "duplicates", "time"])
def test_rehashed_malformed_evidence_is_still_rejected(tmp_path: Path, corruption: str) -> None:
    store, reference = prepared(tmp_path)
    with sqlite3.connect(store.path) as connection:
        content = json.loads(
            connection.execute("SELECT document FROM evaluation_batches").fetchone()[0]
        )
        if corruption == "schema":
            content["schema_version"] = "99"
        elif corruption == "duplicates":
            content["source_batch"]["samples"][1]["observation_id"] = content["source_batch"][
                "samples"
            ][0]["observation_id"]
        else:
            content["source_batch"]["samples"][0]["observed_at"] = END.isoformat()
        payload = json.dumps(content).encode()
        new_ref = "sha256:" + sha256(payload).hexdigest()
        connection.execute(
            "UPDATE evaluation_batches SET document = ?, evidence_ref = ?", (payload, new_ref)
        )
    with pytest.raises(EvidenceIntegrityError, match="schema"):
        store.load_batch(reference.model_copy(update={"evidence_ref": new_ref}))


def test_manifest_digest_and_exact_policy_binding_are_verified(tmp_path: Path) -> None:
    store, reference = prepared(tmp_path)
    with sqlite3.connect(store.path) as connection:
        content = json.loads(
            connection.execute("SELECT document FROM project_manifests").fetchone()[0]
        )
        content["manifest"]["min_f1"] = 0.1
        payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            "UPDATE project_manifests SET document = ?, digest = ?",
            (payload, sha256(payload).hexdigest()),
        )
    with pytest.raises(EvidenceIntegrityError, match="different project policy"):
        store.load_batch(reference)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE project_manifests SET document = ?", (b"changed",))
    with pytest.raises(EvidenceIntegrityError, match="manifest digest"):
        store.get_manifest("tenant-a", reference.project_id, reference.model_id, "v1")


def test_changed_batch_gets_new_identity_without_overwriting_old_evidence(tmp_path: Path) -> None:
    store, previous = prepared(tmp_path)
    manifest, batch = example()
    content = batch.model_dump(mode="json")
    content["samples"][0]["predicted"] = 0
    changed = EvaluationBatch.model_validate(content)
    new = store.save_batch("tenant-a", manifest, changed, START, END)
    assert new.evidence_ref != previous.evidence_ref
    original_report = asyncio.run(
        ProjectEvaluationService(
            manifests=store, batches=store, tenant_id="tenant-a"
        ).evaluate_reference(previous)
    )
    assert original_report.metrics["f1"] == 1.0
    assert store.load_batch(new).samples[0].model_dump()["predicted"] == 0


def test_window_samples_registration_and_size_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    manifest, batch = example()
    with pytest.raises(ValueError, match="registered"):
        store.save_batch("tenant-a", manifest, batch, START, END)
    store.register_manifest("tenant-a", manifest)
    with pytest.raises(ValueError, match="time window"):
        store.save_batch("tenant-a", manifest, batch, START + timedelta(hours=1), END)
    with pytest.raises(ValidationError):
        store.save_batch("tenant-a", manifest, batch, START.replace(tzinfo=None), END)
    reference = store.save_batch("tenant-a", manifest, batch, START, END)
    monkeypatch.setattr(storage_module, "MAX_BATCH_BYTES", 10)
    with pytest.raises(ValueError, match="size"):
        store.save_batch("tenant-a", manifest, batch, START, END)
    with pytest.raises(EvidenceIntegrityError, match="size"):
        store.load_batch(reference)


def test_incompatible_database_schema_and_unavailable_store_fail(tmp_path: Path) -> None:
    store, reference = prepared(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(ProjectStorageUnavailableError, match="schema"):
        SQLiteProjectStore(store.path)
    unrelated = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE other_project (id INTEGER)")
    with pytest.raises(ProjectStorageUnavailableError, match="different application"):
        SQLiteProjectStore(unrelated)
    store.path = tmp_path  # Simulate an unavailable database path.
    with pytest.raises(ProjectStorageUnavailableError, match="operation failed"):
        store.load_batch(reference)
    with pytest.raises(ValueError, match="file database"):
        SQLiteProjectStore(Path(":memory:"))


def test_reference_only_agent_task_and_rejected_inputs(tmp_path: Path) -> None:
    store, reference = prepared(tmp_path)
    agent = MLOpsLifecycleAgent(
        project_evaluation=ProjectEvaluationService(
            manifests=store, batches=store, tenant_id="tenant-a"
        )
    )
    passed = asyncio.run(agent.handle(task(reference)))
    assert passed.status == ResultStatus.SUCCEEDED
    assert passed.evidence_refs == (reference.evidence_ref,)
    assert passed.payload["metrics"] == {"f1": 1.0, "positive_labels": 2.0}
    failed = asyncio.run(agent.handle(task(reference, include_evidence=False)))
    assert failed.status == ResultStatus.FAILED
    wrong_tenant = reference.model_copy(update={"tenant_id": "tenant-b"})
    assert asyncio.run(agent.handle(task(wrong_tenant))).status == ResultStatus.FAILED
    both = task(reference).model_dump(mode="json")
    both["task_context"]["signals"]["project_evaluation"] = example()[1].model_dump(mode="json")
    rejected = asyncio.run(agent.handle(AgentTask.model_validate(both)))
    assert rejected.status == ResultStatus.FAILED
    assert "Choose one" in rejected.rationale_summary


class CountingJudge:
    model_name = "stub:local"

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, _sample: TextEvaluationSample) -> TextJudgment:
        self.calls += 1
        return TextJudgment(score=0.9, rationale="synthetic")


def test_tampered_text_evidence_never_invokes_judge(tmp_path: Path) -> None:
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    manifest, batch = example("knowledge-assistant")
    store.register_manifest("tenant-a", manifest)
    reference = store.save_batch("tenant-a", manifest, batch, START, END)
    judge = CountingJudge()
    service = ProjectEvaluationService(
        manifests=store, batches=store, tenant_id="tenant-a", text_judge=judge
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE evaluation_batches SET document = ?", (b"changed",))
    with pytest.raises(EvidenceIntegrityError, match="digest"):
        asyncio.run(service.evaluate_reference(reference))
    assert judge.calls == 0


def test_cli_registration_ingestion_and_evaluation_in_separate_processes(tmp_path: Path) -> None:
    database = tmp_path / "cli.sqlite3"
    fixture = ROOT / "contracts/examples/agent6/projects"
    command = [sys.executable, "-m", "agent6_mlops_lifecycle.cli.storage"]
    common = ["--database", str(database), "--tenant", "cli-test"]

    def invoke(arguments: list[str]) -> dict:
        result = subprocess.run(
            [*command, *arguments, *common],
            cwd=ROOT.parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    invoke(["register", "--manifest", str(fixture / "support-routing-manifest.json")])
    reference = invoke(
        [
            "ingest",
            "--batch",
            str(fixture / "support-routing-batch.json"),
            "--window-start",
            START.isoformat(),
            "--window-end",
            END.isoformat(),
        ]
    )
    ref_file = tmp_path / "reference.json"
    ref_file.write_text(json.dumps(reference), encoding="utf-8")
    report = invoke(["evaluate", "--reference", str(ref_file)])
    assert report["decision"] == "PASS"
    assert report["evidence_ref"] == reference["evidence_ref"]


def test_reference_binds_policy_even_when_repositories_are_wired_differently(
    tmp_path: Path,
) -> None:
    evidence_store, reference = prepared(tmp_path)
    other_catalog = SQLiteProjectStore(tmp_path / "other-catalog.sqlite3")
    manifest, _batch = example()
    other_catalog.register_manifest("tenant-a", manifest.model_copy(update={"min_f1": 0.1}))
    service = ProjectEvaluationService(
        manifests=other_catalog, batches=evidence_store, tenant_id="tenant-a"
    )
    with pytest.raises(ValueError, match="registered project policy"):
        asyncio.run(service.evaluate_reference(reference))


def test_reference_evaluation_uses_the_verified_policy_snapshot(tmp_path: Path) -> None:
    store, reference = prepared(tmp_path)
    manifest, _batch = example()

    class ChangingCatalog:
        calls = 0

        def register_manifest(self, tenant_id: str, value: ProjectManifest) -> None:
            raise AssertionError("No registration occurs during reference evaluation")

        def get_manifest(
            self, tenant_id: str, project_id: str, model_id: str, model_version: str
        ) -> ProjectManifest:
            self.calls += 1
            return manifest if self.calls == 1 else manifest.model_copy(update={"min_f1": 0.1})

    catalog = ChangingCatalog()
    service = ProjectEvaluationService(manifests=catalog, batches=store, tenant_id="tenant-a")
    report = asyncio.run(service.evaluate_reference(reference))
    assert catalog.calls == 1
    assert "required 0.750" in report.rationale_summary
