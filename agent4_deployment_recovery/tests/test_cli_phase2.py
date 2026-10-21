import json

from agent4_deployment_recovery.cli import main


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_offline_snapshot_inspection_and_patch_preview(
    tmp_path,
    capsys,
    operation_factory,
    snapshot_factory,
    preflight_policy,
):
    operation = operation_factory()
    operation_payload = operation.model_dump(mode="json")
    operation_payload["authorization_token"] = "x" * 24
    operation_path = tmp_path / "operation.json"
    snapshot_path = tmp_path / "snapshot.json"
    policy_path = tmp_path / "policy.json"
    _write(operation_path, operation_payload)
    _write(snapshot_path, snapshot_factory().model_dump(mode="json"))
    _write(policy_path, preflight_policy.model_dump(mode="json"))

    assert main(["inspect", "--snapshot", str(snapshot_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["uid"] == operation.target.uid

    assert (
        main(
            [
                "patch-preview",
                str(operation_path),
                str(snapshot_path),
                str(policy_path),
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run_only"] is True
    assert preview["content_type"] == "application/json-patch+json"


def test_server_dry_run_cli_fails_before_cluster_contact_without_opt_in(capsys):
    result = main(
        [
            "server-dry-run",
            "operation.json",
            "policy.json",
            "connection.json",
        ]
    )
    assert result == 2
    error = json.loads(capsys.readouterr().err)
    assert "--contact-cluster" in error["error"]
