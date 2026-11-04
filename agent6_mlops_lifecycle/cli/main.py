"""Single entry point for the self-contained Agent 6 integration."""

import argparse
import importlib.metadata
import json
import sys

from agent6_mlops_lifecycle.monitoring.settings import ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "run",
            "doctor",
            "monitor",
            "status",
            "stop",
            "train-diagnostic",
            "validate",
            "init",
            "serve",
        ],
    )
    args, remaining = parser.parse_known_args()
    if args.command in ("run", "init", "serve", "validate", "train-diagnostic"):
        from agent6_mlops_lifecycle.paths import require_project_root

        require_project_root()
    if args.command == "run":
        from agent6_mlops_lifecycle.runner import main as run

        run(remaining)
    elif args.command in ("init", "serve"):
        from agent6_mlops_lifecycle.bootstrap import prepare

        config = prepare()
        if args.command == "init":
            print(
                json.dumps(
                    {
                        "status": "PREPARED",
                        "repository": str(ROOT),
                        "benchmark_registry": str(config.benchmark_registry),
                        "secrets_printed": False,
                    }
                )
            )
        else:
            import os

            os.environ["AGENT6_MONITOR_BENCHMARK_REGISTRY"] = str(config.benchmark_registry)
            os.environ.setdefault("AGENT6_MONITOR_BENCHMARK_PYTHON", sys.executable)
            from agent6_mlops_lifecycle.cli.monitor import main as monitor

            sys.argv = [sys.argv[0], *remaining]
            monitor()
    elif args.command == "validate":
        from agent6_mlops_lifecycle.validation import main as validate

        validate()
    elif args.command == "train-diagnostic":
        from agent6_mlops_lifecycle.cli.train import main as train

        train()
    elif args.command == "doctor":
        from agent6_mlops_lifecycle.evaluation.llm_judge import LLMJudgeSettings
        from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings

        config = MonitoringSettings()
        versions = {}
        for name in (
            "pydantic",
            "pydantic-settings",
            "mlflow",
            "scikit-learn",
            "imbalanced-learn",
            "streamlit",
        ):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = "NOT_INSTALLED"
        print(
            json.dumps(
                {
                    "repository": str(ROOT),
                    "python": sys.version.split()[0],
                    "versions": versions,
                    "groq_key_configured": bool(LLMJudgeSettings().groq_api_key.get_secret_value()),
                    "source_database_exists": config.source_database.exists(),
                    "external_repository_required": False,
                },
                indent=2,
            )
        )
    elif args.command == "stop":
        from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings

        path = MonitoringSettings().state_database.parent / "stop.request"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        print("Graceful stop requested for owned services.")
    elif args.command == "status":
        from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
        from agent6_mlops_lifecycle.monitoring.state import MonitorState

        config = MonitoringSettings()
        state = MonitorState(config.state_database, config.scope()).status()
        print(state.model_dump_json(indent=2) if state else '{"state":"NOT_STARTED"}')
    else:
        from agent6_mlops_lifecycle.cli.monitor import main as monitor

        sys.argv = [sys.argv[0], *remaining]
        monitor()
