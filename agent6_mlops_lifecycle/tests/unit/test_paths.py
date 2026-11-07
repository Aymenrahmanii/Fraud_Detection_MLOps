"""Installed packages resolve explicit projects and keep configuration separate."""

import os
import subprocess
import sys

from agent6_mlops_lifecycle.paths import discover_project_root


def test_explicit_project_root_overrides_install_location(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT6_PROJECT_ROOT", str(tmp_path))
    assert discover_project_root() == tmp_path.resolve()


def test_custom_environment_filename_is_used_by_all_configuration_readers(tmp_path):
    config = tmp_path / "private-agent6.config"
    config.write_text(
        "AGENT6_MONITOR_WINDOW_SECONDS=1234\n"
        "AGENT6_LIFECYCLE_TIMEOUT_SECONDS=123\n"
        "AGENT6_LLM_MODEL=custom-test-model\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    for key in ("AGENT6_MONITOR_WINDOW_SECONDS", "AGENT6_LIFECYCLE_TIMEOUT_SECONDS"):
        env.pop(key, None)
    env["AGENT6_ENV_FILE"] = str(config)
    script = """
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.lifecycle.settings import LifecycleSettings
from agent6_mlops_lifecycle.evaluation.llm_judge import LLMJudgeSettings
from agent6_mlops_lifecycle.paths import ENV_FILE
assert MonitoringSettings().window_seconds == 1234
assert LifecycleSettings().timeout_seconds == 123
assert LLMJudgeSettings.model_config['env_file'] == ENV_FILE
"""
    subprocess.run([sys.executable, "-B", "-c", script], env=env, check=True, timeout=20)
