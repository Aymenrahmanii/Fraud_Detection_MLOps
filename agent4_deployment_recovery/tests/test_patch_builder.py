import pytest
from pydantic import ValidationError

from agent4_deployment_recovery.core.errors import JournalConflictError
from agent4_deployment_recovery.core.kubernetes_models import (
    JsonPatchOperation,
    PreflightDecision,
)
from agent4_deployment_recovery.core.patch_builder import (
    build_typed_patch,
    escape_json_pointer_token,
)
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.schemas import (
    OperationType,
    ScaleReplicas,
    SetContainerResources,
)


def test_json_pointer_token_escaping_handles_tilde_before_slash():
    assert escape_json_pointer_token("container~/name") == "container~0~1name"


def test_json_patch_contract_rejects_arbitrary_container_subpaths():
    with pytest.raises(ValidationError, match="outside the typed Deployment allowlist"):
        JsonPatchOperation(
            op="replace",
            path="/spec/template/spec/containers/0/env/0/value",
            value="forbidden",
        )


def test_image_patch_is_minimal_guarded_and_has_exact_inverse(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory()
    preflight = run_preflight(operation, snapshot, preflight_policy)
    preview = build_typed_patch(operation, snapshot, preflight)

    assert [(item.op, item.path) for item in preview.patch] == [
        ("test", "/metadata/uid"),
        ("test", "/metadata/resourceVersion"),
        ("test", "/spec/template/spec/containers/0/name"),
        ("test", "/spec/template/spec/containers/0/image"),
        ("replace", "/spec/template/spec/containers/0/image"),
    ]
    assert preview.rollback.prior_image == snapshot.containers[0].image
    assert preview.rollback.container_index == 0
    assert preview.dry_run_only is True
    assert (
        build_typed_patch(operation, snapshot, preflight).patch_hash
        == preview.patch_hash
    )


def test_scale_patch_tests_prior_replicas_and_captures_inverse(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory(
        operation=ScaleReplicas(
            operation_type=OperationType.SCALE_REPLICAS,
            from_replicas=2,
            to_replicas=3,
        )
    )
    snapshot = snapshot_factory(replicas=2)
    preflight = run_preflight(operation, snapshot, preflight_policy)
    preview = build_typed_patch(operation, snapshot, preflight)

    assert preview.patch[-2].model_dump() == {
        "op": "test",
        "path": "/spec/replicas",
        "value": 2,
    }
    assert preview.patch[-1].model_dump() == {
        "op": "replace",
        "path": "/spec/replicas",
        "value": 3,
    }
    assert preview.rollback.prior_replicas == 2


def test_resource_patch_preserves_unspecified_values_and_exact_snapshot(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory(
        operation=SetContainerResources(
            operation_type=OperationType.SET_CONTAINER_RESOURCES,
            memory_limit_mib=512,
        )
    )
    snapshot = snapshot_factory()
    preflight = run_preflight(operation, snapshot, preflight_policy)
    preview = build_typed_patch(operation, snapshot, preflight)
    prior = snapshot.containers[0].raw["resources"]

    assert preview.patch[-2].op == "test"
    assert preview.patch[-2].value == prior
    assert preview.patch[-1].op == "replace"
    assert preview.patch[-1].value == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "200m", "memory": "512Mi"},
    }
    assert preview.rollback.prior_resources == prior
    assert preview.rollback.resources_present is True


def test_absent_resources_use_metadata_and_name_tests_before_bounded_add(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory(
        operation=SetContainerResources(
            operation_type=OperationType.SET_CONTAINER_RESOURCES,
            cpu_request_millicores=100,
        )
    )
    snapshot = snapshot_factory(resources_present=False)
    preflight = run_preflight(operation, snapshot, preflight_policy)
    preview = build_typed_patch(operation, snapshot, preflight)

    assert preflight.decision == PreflightDecision.PASS
    assert preview.patch[-2].path == "/spec/template/spec/containers/0/name"
    assert preview.patch[-2].value == "fraud-api"
    assert preview.patch[-1].op == "add"
    assert preview.patch[-1].path.endswith("/resources")
    assert preview.rollback.prior_resources is None
    assert preview.rollback.resources_present is False


def test_patch_builder_refuses_blocked_preflight(
    operation_factory, snapshot_factory, preflight_policy
):
    operation = operation_factory()
    snapshot = snapshot_factory(readiness_probe=False)
    preflight = run_preflight(operation, snapshot, preflight_policy)
    with pytest.raises(JournalConflictError, match="passing preflight"):
        build_typed_patch(operation, snapshot, preflight)
