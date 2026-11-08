"""Read-only tools for Agent 2's investigator, over the simulated cluster.

Each tool receives the service and namespace from the task, never from the
model, so a model cannot look at any other service. The tools expose state the
rules cannot see: the release history with timings, and restart reasons.
"""

import os
from collections.abc import Callable
from typing import Any

from aiops_platform.llm.client import GeminiClient, LLMError
from aiops_platform.llm.investigator import Investigator

DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_current_signals",
        "description": "Live signals of the service under review: memory use and growth, CPU, error rate, p95 latency, "
        "pod restarts, and minutes since its current release rolled out.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "get_release_history",
        "description": "The service's recent releases, each with how many minutes ago it rolled out, and the version "
        "now running.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "get_restart_events",
        "description": "Container restart count and the last termination reason for the service's pods.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]


def make_sim_tools(cluster: Any) -> dict[str, Callable[[str, str], dict[str, Any]]]:
    def current_signals(service: str, namespace: str) -> dict[str, Any]:
        return {key: round(float(value), 3) for key, value in cluster.signals(service, namespace).items()}

    def release_history(service: str, namespace: str) -> dict[str, Any]:
        dep = cluster.deployment(service, namespace)
        return {
            "running_version": dep.version,
            "releases": [{"version": v, "minutes_ago": round(cluster.minute - m, 1)} for m, v in dep.history[-5:]],
        }

    def restart_events(service: str, namespace: str) -> dict[str, Any]:
        dep = cluster.deployment(service, namespace)
        return {
            "restart_count": dep.restarts,
            "last_termination_reason": "OOMKilled" if dep.restarts else None,
            "replicas": dep.replicas,
        }

    return {
        "get_current_signals": current_signals,
        "get_release_history": release_history,
        "get_restart_events": restart_events,
    }


def gemini_investigator_from_env(cluster: Any) -> Investigator | None:
    """Enabled only when AIOPS_AGENT2_LLM=gemini. A misconfigured switch fails at start-up, not mid-incident."""
    if os.environ.get("AIOPS_AGENT2_LLM", "off").lower() != "gemini":
        return None
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise LLMError("AIOPS_AGENT2_LLM=gemini needs GEMINI_API_KEY in the environment")
    client = GeminiClient(model=os.environ.get("AIOPS_AGENT2_MODEL", "gemini-3.8-flash"), api_key=key)
    return Investigator(client, make_sim_tools(cluster), DECLARATIONS)
