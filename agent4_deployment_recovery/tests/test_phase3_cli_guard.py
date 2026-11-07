import argparse

from agent4_deployment_recovery.cli import _parser


def test_public_cli_exposes_no_live_mutation_or_rollback_command():
    parser = _parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)
    assert not commands.intersection(
        {"apply", "execute", "deploy", "rollback", "recover", "kubectl"}
    )
    assert commands == {
        "validate-operation",
        "plan",
        "validate-plan",
        "validate-agent3",
        "validate-agent5",
        "inspect",
        "preflight",
        "patch-preview",
        "server-dry-run",
    }
