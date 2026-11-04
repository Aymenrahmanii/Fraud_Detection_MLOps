"""Source validation, delayed outcomes, replay and durable ingestion boundaries."""

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

import agent6_mlops_lifecycle.adapters.jsonl_observations as jsonl_module
import agent6_mlops_lifecycle.ingestion.builder as builder_module
from agent6_mlops_lifecycle.adapters.jsonl_observations import JSONLObservationSource
from agent6_mlops_lifecycle.adapters.sqlite_projects import SQLiteProjectStore
from agent6_mlops_lifecycle.cli.ingest import ingest_sources
from agent6_mlops_lifecycle.cli.ingestion_demo import run
from agent6_mlops_lifecycle.contracts.mlops_ingestion import (
    LocalObservationSourceConfig,
    ObservationIngestionReceipt,
    ObservationIngestionRequest,
    OutcomeRecord,
    PredictionRecord,
)
from agent6_mlops_lifecycle.contracts.mlops_projects import ProjectManifest
from agent6_mlops_lifecycle.evaluation.engine import ProjectEvaluationService
from agent6_mlops_lifecycle.ingestion.builder import build_observation_window
from agent6_mlops_lifecycle.ingestion.service import ObservationIngestionService

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts/examples/agent6/ingestion"


def fixture(name="support-routing", phase="early"):
    manifest = ProjectManifest.model_validate_json(
        (ROOT / f"contracts/examples/agent6/projects/{name}-manifest.json").read_bytes()
    )
    config = LocalObservationSourceConfig.model_validate_json(
        (FIXTURES / f"{name}-sources-{phase}.json").read_bytes()
    )
    source = JSONLObservationSource(
        FIXTURES / config.predictions_file, FIXTURES / config.outcomes_file
    )
    return manifest, config.query, source.read_snapshot()


def source_files(tmp_path, snapshot):
    predictions = tmp_path / "predictions.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"
    for path, records in ((predictions, snapshot.predictions), (outcomes, snapshot.outcomes)):
        path.write_text(
            "".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8"
        )
    return JSONLObservationSource(predictions, outcomes)


def prepared(tmp_path, name="support-routing"):
    manifest, query, snapshot = fixture(name)
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    store.register_manifest("demo", manifest)
    service = ObservationIngestionService(manifests=store, batches=store, tenant_id="demo")
    return store, service, query, source_files(tmp_path, snapshot)


@pytest.mark.parametrize("name", ["support-routing", "energy-demand"])
def test_delayed_outcomes_change_only_the_later_snapshot_and_old_evidence_survives(tmp_path, name):
    store, service, query, source = prepared(tmp_path, name)
    early = service.ingest(query, source)
    late_query = query.model_copy(update={"as_of": datetime(2026, 10, 3, tzinfo=UTC)})
    late = service.ingest(late_query, source)
    assert early.summary.matched_count == 2
    assert early.summary.late_outcome_count == 2
    assert early.summary.prediction_count == 5
    assert early.summary.duplicate_prediction_count == 1
    assert early.summary.outcome_coverage == 0.4
    assert early.summary.pending_observation_ids == (
        "observation-3",
        "observation-4",
        "observation-5",
    )
    assert late.summary.matched_count == 4
    assert late.summary.outcome_coverage == 0.8
    assert late.summary.pending_observation_ids == ("observation-5",)
    assert early.reference != late.reference
    assert early.reference is not None and late.reference is not None
    reopened = SQLiteProjectStore(store.path)
    evaluator = ProjectEvaluationService(manifests=reopened, batches=reopened, tenant_id="demo")
    first = asyncio.run(evaluator.evaluate_reference(early.reference))
    later = asyncio.run(evaluator.evaluate_reference(late.reference))
    assert first.decision == "INSUFFICIENT_EVIDENCE"
    assert later.decision == "PASS"
    assert asyncio.run(evaluator.evaluate_reference(early.reference)) == first
    assert service.ingest(query, source) == early
    assert first.requires_human_review and later.requires_human_review
    if name == "support-routing":
        assert later.metrics["f1"] == 1.0
    else:
        assert later.metrics["mae"] == pytest.approx(0.1)


def test_append_missing_outcome_and_replay_keeps_earlier_reference(tmp_path):
    store, service, query, source = prepared(tmp_path)
    later = query.model_copy(update={"as_of": datetime(2026, 10, 3, tzinfo=UTC)})
    before = service.ingest(later, source)
    manifest, _, snapshot = fixture()
    fifth = OutcomeRecord(
        tenant_id="demo",
        project_id=manifest.project_id,
        model_id=manifest.model_id,
        model_version=manifest.model_version,
        task_type="binary_classification",
        observation_id="observation-5",
        source_ref="synthetic:new-confirmation",
        confirmed_at=datetime(2026, 10, 2, 12, tzinfo=UTC),
        actual=0,
    )
    with source.outcomes_file.open("a", encoding="utf-8") as stream:
        stream.write(fifth.model_dump_json() + "\n")
    after = service.ingest(later, source)
    assert before.reference is not None and after.reference is not None
    assert before.reference.evidence_ref != after.reference.evidence_ref
    assert len(store.load_batch(before.reference).samples) == 4
    assert len(store.load_batch(after.reference).samples) == 5
    assert after.summary.outcome_coverage == 1.0
    assert after.summary.pending_observation_ids == ()


def test_no_answers_returns_insufficient_evidence_without_an_empty_batch(tmp_path):
    store, service, query, source = prepared(tmp_path)
    source.outcomes_file.write_bytes(b"")
    receipt = service.ingest(query, source)
    assert receipt.status == "INSUFFICIENT_EVIDENCE"
    assert receipt.reference is None
    assert receipt.summary.outcome_coverage == 0
    assert receipt.summary.matched_count == 0
    assert len(receipt.summary.pending_observation_ids) == 5


def test_empty_window_has_no_coverage_or_fake_samples():
    manifest, query, snapshot = fixture()
    changed = query.model_copy(
        update={
            "window_start": query.window_start - timedelta(days=2),
            "window_end": query.window_start - timedelta(days=1),
        }
    )
    built = build_observation_window(changed, manifest, snapshot)
    assert built.batch is None
    assert built.summary.available_prediction_count == 0
    assert built.summary.outcome_coverage == 0


def test_event_window_is_half_open_and_cutoff_is_inclusive():
    manifest, query, snapshot = fixture()
    start = query.window_start
    first = snapshot.predictions[0].model_copy(
        update={
            "observed_at": start,
            "recorded_at": query.as_of,
        }
    )
    boundary = snapshot.predictions[1].model_copy(
        update={
            "observed_at": query.window_end,
            "recorded_at": query.window_end,
        }
    )
    outcome = snapshot.outcomes[-1].model_copy(update={"confirmed_at": query.as_of})
    built = build_observation_window(
        query,
        manifest,
        replace(snapshot, predictions=(boundary, first), outcomes=(outcome,)),
    )
    assert built.summary.window_prediction_count == 1
    assert built.summary.matched_count == 1
    assert built.batch is not None
    assert built.batch.samples[0].observation_id == first.observation_id


def test_late_prediction_is_not_available_even_if_its_answer_exists():
    manifest, query, snapshot = fixture()
    first = snapshot.predictions[0].model_copy(
        update={"recorded_at": query.as_of + timedelta(seconds=1)}
    )
    built = build_observation_window(
        query, manifest, replace(snapshot, predictions=(first, *snapshot.predictions[1:5]))
    )
    assert built.summary.late_prediction_count == 1
    assert built.summary.available_prediction_count == 4
    assert built.summary.matched_count == 1
    assert built.summary.outcome_coverage == 0.25
    assert "observation-1" not in built.summary.pending_observation_ids


def test_record_order_does_not_control_pairing():
    manifest, query, snapshot = fixture(phase="late")
    normal = build_observation_window(query, manifest, snapshot)
    reversed_rows = replace(
        snapshot,
        predictions=tuple(reversed(snapshot.predictions)),
        outcomes=tuple(reversed(snapshot.outcomes)),
    )
    assert build_observation_window(query, manifest, reversed_rows) == normal


@pytest.mark.parametrize("kind", ["predictions", "outcomes"])
def test_identical_duplicates_are_ignored_but_conflicts_are_rejected(kind):
    manifest, query, snapshot = fixture()
    records = getattr(snapshot, kind)
    duplicate = replace(snapshot, **{kind: (*records, records[0])})
    built = build_observation_window(query, manifest, duplicate)
    field = "duplicate_prediction_count" if kind == "predictions" else "duplicate_outcome_count"
    assert getattr(built.summary, field) == (2 if kind == "predictions" else 1)
    value_field = "predicted" if kind == "predictions" else "actual"
    conflicting = records[0].model_copy(update={value_field: 1 - getattr(records[0], value_field)})
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        build_observation_window(
            query, manifest, replace(snapshot, **{kind: (*records, conflicting)})
        )


@pytest.mark.parametrize("kind", ["predictions", "outcomes"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other"),
        ("project_id", "other"),
        ("model_id", "other"),
        ("model_version", "v2"),
        ("task_type", "regression"),
    ],
)
def test_mixed_scope_or_task_fails_even_outside_the_requested_window(kind, field, value):
    manifest, query, snapshot = fixture()
    records = getattr(snapshot, kind)
    wrong = records[0].model_copy(update={field: value})
    with pytest.raises(ValueError, match="differs from scope"):
        build_observation_window(
            query, manifest, replace(snapshot, **{kind: (wrong, *records[1:])})
        )


def test_unknown_outcome_and_confirmation_before_prediction_fail():
    manifest, query, snapshot = fixture()
    for outcome, message in (
        (snapshot.outcomes[0].model_copy(update={"observation_id": "unknown"}), "no corresponding"),
        (snapshot.outcomes[0].model_copy(update={"confirmed_at": query.window_start}), "precede"),
    ):
        with pytest.raises(ValueError, match=message):
            build_observation_window(query, manifest, replace(snapshot, outcomes=(outcome,)))


@pytest.mark.parametrize("value", [True, 0.5, 1.0, "1", 2, float("nan"), float("inf")])
def test_binary_values_are_strict_labels(value):
    _, _, snapshot = fixture()
    for record, field in ((snapshot.predictions[0], "predicted"), (snapshot.outcomes[0], "actual")):
        with pytest.raises(ValidationError):
            type(record).model_validate({**record.model_dump(), field: value})


@pytest.mark.parametrize("value", [True, "1.2", float("nan"), float("inf")])
def test_regression_values_reject_coercions_and_nonfinite_numbers(value):
    _, _, snapshot = fixture("energy-demand")
    with pytest.raises(ValidationError):
        PredictionRecord.model_validate(
            {
                **snapshot.predictions[0].model_dump(),
                "predicted": value,
            }
        )


def test_record_and_query_timestamps_are_aware_and_consistent():
    _, query, snapshot = fixture()
    with pytest.raises(ValidationError):
        PredictionRecord.model_validate(
            {
                **snapshot.predictions[0].model_dump(),
                "observed_at": datetime(2026, 9, 30),
            }
        )
    with pytest.raises(ValidationError, match="recorded_at"):
        PredictionRecord.model_validate(
            {
                **snapshot.predictions[0].model_dump(),
                "recorded_at": query.window_start,
            }
        )
    for update in (
        {"window_end": query.window_start},
        {"as_of": query.window_start},
        {"window_start": datetime(2026, 9, 30)},
    ):
        with pytest.raises(ValidationError):
            ObservationIngestionRequest.model_validate({**query.model_dump(), **update})


def test_wrong_tenant_or_missing_model_stops_before_reading_source(tmp_path):
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    service = ObservationIngestionService(manifests=store, batches=store, tenant_id="demo")
    _, query, _ = fixture()

    class NeverRead:
        def read_snapshot(self):
            raise AssertionError("Source must not be read")

    with pytest.raises(ValueError, match="configured service tenant"):
        service.ingest(query.model_copy(update={"tenant_id": "other"}), NeverRead())
    with pytest.raises(ValueError, match="Register the exact"):
        service.ingest(query, NeverRead())


def test_unrelated_manifest_and_unsupported_task_fail():
    manifest, query, snapshot = fixture()
    with pytest.raises(ValueError, match="manifest differs"):
        build_observation_window(
            query, manifest.model_copy(update={"model_version": "v2"}), snapshot
        )
    custom = ProjectManifest(
        project_id=query.project_id,
        model_id=query.model_id,
        model_version=query.model_version,
        task_type="custom",
        evaluator_id="custom-v1",
        policy_version="v1",
    )
    with pytest.raises(ValueError, match="currently supports"):
        build_observation_window(query, custom, snapshot)


def test_raw_source_digests_identify_the_exact_bytes_read(tmp_path):
    _, _, snapshot = fixture()
    source = source_files(tmp_path, snapshot)
    loaded = source.read_snapshot()
    assert (
        loaded.prediction_source_ref
        == f"sha256:{sha256(source.predictions_file.read_bytes()).hexdigest()}"
    )
    assert (
        loaded.outcome_source_ref
        == f"sha256:{sha256(source.outcomes_file.read_bytes()).hexdigest()}"
    )
    source.predictions_file.write_bytes(source.predictions_file.read_bytes() + b"\n")
    assert source.read_snapshot().prediction_source_ref != loaded.prediction_source_ref


@pytest.mark.parametrize(
    "content",
    [
        b'{"predicted":0,"predicted":1}\n',
        b'{"predicted":NaN}\n',
        b"[]\n",
        b"not-json\n",
        b"\xff\n",
    ],
)
def test_invalid_json_lines_fail_without_echoing_input(tmp_path, content):
    predictions = tmp_path / "predictions.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"
    predictions.write_bytes(content)
    outcomes.write_bytes(b"")
    with pytest.raises(ValueError, match="Invalid PredictionRecord at source line 1") as error:
        JSONLObservationSource(predictions, outcomes).read_snapshot()
    assert error.value.args == ("Invalid PredictionRecord at source line 1",)


@pytest.mark.parametrize(
    "setting,limit,message",
    [
        ("MAX_SOURCE_BYTES", 2, "source exceeds the byte"),
        ("MAX_SOURCE_RECORDS", 1, "record limit"),
        ("MAX_LINE_BYTES", 2, "line 1 exceeds"),
    ],
)
def test_source_resource_limits(tmp_path, monkeypatch, setting, limit, message):
    _, _, snapshot = fixture()
    source = source_files(tmp_path, snapshot)
    monkeypatch.setattr(jsonl_module, setting, limit)
    with pytest.raises(ValueError, match=message):
        source.read_snapshot()


def test_window_limit_and_bad_source_digest_fail(monkeypatch):
    manifest, query, snapshot = fixture()
    monkeypatch.setattr(builder_module, "MAX_WINDOW_PREDICTIONS", 4)
    with pytest.raises(ValueError, match="window exceeds"):
        build_observation_window(query, manifest, snapshot)
    monkeypatch.setattr(builder_module, "MAX_WINDOW_PREDICTIONS", 20_000)
    with pytest.raises(ValidationError):
        build_observation_window(
            query, manifest, replace(snapshot, prediction_source_ref="local:unverified")
        )


def test_receipt_rejects_inconsistent_counts_and_reference_scope(tmp_path):
    _, service, query, source = prepared(tmp_path)
    receipt = service.ingest(query, source)
    assert receipt.reference is not None
    raw = receipt.model_dump()
    raw["summary"]["outcome_coverage"] = 1.0
    with pytest.raises(ValidationError, match="coverage differs"):
        ObservationIngestionReceipt.model_validate(raw)
    raw = receipt.model_dump()
    raw["reference"]["project_id"] = "other"
    with pytest.raises(ValidationError, match="reference differs"):
        ObservationIngestionReceipt.model_validate(raw)


def test_cli_reuses_registration_and_resolves_relative_paths_in_separate_processes(tmp_path):
    manifest, query, snapshot = fixture()
    store = SQLiteProjectStore(tmp_path / "projects.sqlite3")
    store.register_manifest("demo", manifest)
    source = source_files(tmp_path, snapshot)
    config = LocalObservationSourceConfig(
        query=query,
        predictions_file=Path(source.predictions_file.name),
        outcomes_file=Path(source.outcomes_file.name),
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "agent6_mlops_lifecycle.cli.ingest",
        "--sources",
        str(config_path),
        "--database",
        str(store.path),
        "--tenant",
        "demo",
        "--evaluate",
    ]
    result = subprocess.run(command, cwd=ROOT.parent, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    receipt = ObservationIngestionReceipt.model_validate(payload["ingestion"])
    assert receipt.summary.outcome_coverage == 0.4
    assert payload["evaluation"]["decision"] == "INSUFFICIENT_EVIDENCE"
    reference_file = tmp_path / "reference.json"
    reference_file.write_text(receipt.reference.model_dump_json(), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent6_mlops_lifecycle.cli.storage",
            "evaluate",
            "--database",
            str(store.path),
            "--tenant",
            "demo",
            "--reference",
            str(reference_file),
        ],
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == payload["evaluation"]
    assert ingest_sources(config_path, store.path, "demo") == receipt


def test_demo_proves_two_task_families_without_external_services(tmp_path):
    report = asyncio.run(run(tmp_path))
    rows = report["projects"]
    assert [row["decision"] for row in rows] == [
        "INSUFFICIENT_EVIDENCE",
        "PASS",
        "INSUFFICIENT_EVIDENCE",
        "PASS",
    ]
    assert [row["outcome_coverage"] for row in rows] == [0.4, 0.8, 0.4, 0.8]
    assert len(list(tmp_path.glob("*-reference.json"))) == 4


def test_exported_record_schemas_validate_fixtures_and_reject_binary_scores():
    from jsonschema import Draft202012Validator

    for model, kind in ((PredictionRecord, "predictions"), (OutcomeRecord, "outcomes")):
        schema = json.loads(
            (ROOT / f"contracts/jsonschema/v1/{model.__name__}.schema.json").read_text()
        )
        validator = Draft202012Validator(schema)
        for name in ("support-routing", "energy-demand"):
            _, _, snapshot = fixture(name)
            for record in getattr(snapshot, kind):
                validator.validate(record.model_dump(mode="json"))
        _, _, snapshot = fixture()
        row = getattr(snapshot, kind)[0].model_dump(mode="json")
        field = "predicted" if model is PredictionRecord else "actual"
        for bad_value in (True, 0.5, "1", 2):
            assert list(validator.iter_errors({**row, field: bad_value}))
