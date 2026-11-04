"""Prepare repository-local configuration and immutable benchmark catalog."""

import secrets

from agent6_mlops_lifecycle.assurance.bootstrap import configure_fraud_benchmark
from agent6_mlops_lifecycle.cli.monitor import ensure_monitor_token
from agent6_mlops_lifecycle.lifecycle.settings import LifecycleSettings
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.paths import ENV_FILE, ROOT


def prepare() -> MonitoringSettings:
    config = ensure_monitor_token()
    if len(LifecycleSettings().integration_token.get_secret_value()) < 32:
        with ENV_FILE.open("a", encoding="utf-8") as stream:
            stream.write("\nAGENT6_LIFECYCLE_INTEGRATION_TOKEN=" + secrets.token_urlsafe(32) + "\n")
    registry = configure_fraud_benchmark(
        ROOT,
        config.benchmark_database,
        config.state_database.parent / "benchmark-registry.json",
        config.scope(),
        include_streamlit=True,
    )
    return config.model_copy(update={"benchmark_registry": registry})
