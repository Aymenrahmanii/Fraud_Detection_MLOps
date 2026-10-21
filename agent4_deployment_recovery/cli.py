"""Read-only and explicitly opted-in dry-run CLI for Agent 4."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from agent4_deployment_recovery.adapters.compatibility import (
    read_agent3_assessment,
    read_agent5_recommendation,
)
from agent4_deployment_recovery.adapters.kubernetes import OfficialKubernetesAdapter
from agent4_deployment_recovery.core.dry_run import server_side_dry_run
from agent4_deployment_recovery.core.errors import Agent4Error
from agent4_deployment_recovery.core.kubernetes_models import (
    DeploymentSnapshot,
    KubernetesConnectionConfig,
    PreflightDecision,
    PreflightPolicy,
)
from agent4_deployment_recovery.core.patch_builder import build_typed_patch
from agent4_deployment_recovery.core.planner import create_plan
from agent4_deployment_recovery.core.preflight import run_preflight
from agent4_deployment_recovery.core.schemas import AuthorizedOperation, DeploymentPlan


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent4",
        description="Validate Agent 4 contracts and inspect dry-run-only plans",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate_operation = commands.add_parser("validate-operation")
    validate_operation.add_argument("input", type=Path)

    plan = commands.add_parser("plan")
    plan.add_argument("input", type=Path)

    validate_plan = commands.add_parser("validate-plan")
    validate_plan.add_argument("input", type=Path)

    validate_agent3 = commands.add_parser("validate-agent3")
    validate_agent3.add_argument("input", type=Path)

    validate_agent5 = commands.add_parser("validate-agent5")
    validate_agent5.add_argument("input", type=Path)

    inspect = commands.add_parser("inspect")
    inspect_source = inspect.add_mutually_exclusive_group(required=True)
    inspect_source.add_argument("--snapshot", type=Path)
    inspect_source.add_argument("--operation", type=Path)
    inspect.add_argument("--connection", type=Path)
    inspect.add_argument("--contact-cluster", action="store_true")

    preflight = commands.add_parser("preflight")
    preflight.add_argument("operation", type=Path)
    preflight.add_argument("snapshot", type=Path)
    preflight.add_argument("policy", type=Path)
    preflight.add_argument("--strategy")

    patch_preview = commands.add_parser("patch-preview")
    patch_preview.add_argument("operation", type=Path)
    patch_preview.add_argument("snapshot", type=Path)
    patch_preview.add_argument("policy", type=Path)

    dry_run = commands.add_parser("server-dry-run")
    dry_run.add_argument("operation", type=Path)
    dry_run.add_argument("policy", type=Path)
    dry_run.add_argument("connection", type=Path)
    dry_run.add_argument("--contact-cluster", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    exit_code = 0
    try:
        if args.command == "validate-operation":
            raw = _read(args.input)
            operation = AuthorizedOperation.model_validate_json(raw)
            result = {
                "valid": True,
                "operation_id": operation.operation_id,
                "operation_type": operation.operation.operation_type,
                "target": operation.target.model_dump(mode="json"),
                "live_mutation_available": False,
            }
        elif args.command == "plan":
            raw = _read(args.input)
            operation = AuthorizedOperation.model_validate_json(raw)
            result = create_plan(operation).model_dump(mode="json")
        elif args.command == "validate-plan":
            raw = _read(args.input)
            plan = DeploymentPlan.model_validate_json(raw)
            result = {
                "valid": True,
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "dry_run_only": plan.dry_run_only,
            }
        elif args.command == "validate-agent3":
            raw = _read(args.input)
            result = read_agent3_assessment(raw).model_dump(mode="json")
        elif args.command == "validate-agent5":
            raw = _read(args.input)
            result = read_agent5_recommendation(raw).model_dump(mode="json")
        elif args.command == "inspect":
            if args.snapshot is not None:
                snapshot = DeploymentSnapshot.model_validate_json(_read(args.snapshot))
            else:
                if not args.contact_cluster:
                    raise ValueError("cluster inspection requires --contact-cluster")
                if args.connection is None:
                    raise ValueError("cluster inspection requires --connection")
                operation = AuthorizedOperation.model_validate_json(
                    _read(args.operation)
                )
                connection = KubernetesConnectionConfig.model_validate_json(
                    _read(args.connection)
                )
                adapter = OfficialKubernetesAdapter.from_config(connection)
                snapshot = adapter.inspect_deployment(operation.target)
            result = snapshot.model_dump(mode="json")
        elif args.command in {"preflight", "patch-preview"}:
            operation = AuthorizedOperation.model_validate_json(_read(args.operation))
            snapshot = DeploymentSnapshot.model_validate_json(_read(args.snapshot))
            policy = PreflightPolicy.model_validate_json(_read(args.policy))
            strategy = args.strategy if args.command == "preflight" else None
            preflight = run_preflight(
                operation,
                snapshot,
                policy,
                requested_strategy=strategy,
            )
            if (
                args.command == "preflight"
                or preflight.decision == PreflightDecision.BLOCK
            ):
                result = preflight.model_dump(mode="json")
                if preflight.decision == PreflightDecision.BLOCK:
                    exit_code = 3
            else:
                result = build_typed_patch(operation, snapshot, preflight).model_dump(
                    mode="json"
                )
        elif args.command == "server-dry-run":
            if not args.contact_cluster:
                raise ValueError("server-side dry-run requires --contact-cluster")
            operation = AuthorizedOperation.model_validate_json(_read(args.operation))
            policy = PreflightPolicy.model_validate_json(_read(args.policy))
            connection = KubernetesConnectionConfig.model_validate_json(
                _read(args.connection)
            )
            adapter = OfficialKubernetesAdapter.from_config(connection)
            snapshot = adapter.inspect_deployment(operation.target)
            preflight = run_preflight(operation, snapshot, policy)
            if preflight.decision == PreflightDecision.BLOCK:
                result = preflight.model_dump(mode="json")
                exit_code = 3
            else:
                preview = build_typed_patch(operation, snapshot, preflight)
                dry_run = server_side_dry_run(
                    adapter,
                    operation,
                    preview,
                    contact_cluster=True,
                )
                result = dry_run.model_dump(mode="json")
                if not dry_run.admission_accepted:
                    exit_code = 3
        else:  # argparse prevents this; retained as a fail-closed guard.
            raise ValueError(f"unsupported command: {args.command}")
    except (Agent4Error, OSError, ValueError, ValidationError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
