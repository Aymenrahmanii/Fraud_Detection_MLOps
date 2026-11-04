"""Resolve this fraud checkout explicitly, including installed-wheel deployments."""

import os
import sys
from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parent


def discover_project_root() -> Path:
    configured = os.environ.get("AGENT6_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = [PACKAGE_DIRECTORY.parent, Path.cwd(), *Path.cwd().parents]
    for candidate in candidates:
        if (candidate / "API/services.py").is_file() and (candidate / "Src/model.py").is_file():
            return candidate.resolve()
    # Pure evaluator/library usage does not require a fraud checkout.
    return Path.cwd().resolve()


ROOT = discover_project_root()
ENV_FILE = (
    Path(os.environ.get("AGENT6_ENV_FILE", str(ROOT / "agent6_mlops_lifecycle/.env")))
    .expanduser()
    .resolve()
)
AGENT_DIRECTORY = ENV_FILE.parent


def require_project_root() -> None:
    if not (ROOT / "API/services.py").is_file() or not (ROOT / "Src/model.py").is_file():
        raise ValueError(
            "Set AGENT6_PROJECT_ROOT to the integrated fraud checkout before running services"
        )
    # Installed CLI use can start outside the checkout. Producer adapters and the
    # original serialized models still need their explicitly configured project.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
