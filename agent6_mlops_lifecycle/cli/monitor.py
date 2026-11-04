"""Run the continuous Agent 6 monitor and authenticated localhost dashboard."""

import argparse
import secrets
from pathlib import Path

import uvicorn

from agent6_mlops_lifecycle.monitoring.app import create_app
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.paths import ENV_FILE


def ensure_monitor_token() -> MonitoringSettings:
    settings = MonitoringSettings()
    missing = []
    if len(settings.token.get_secret_value()) < 32:
        missing.append("AGENT6_MONITOR_TOKEN=" + secrets.token_urlsafe(32))
    if len(settings.outcome_token.get_secret_value()) < 32:
        missing.append("AGENT6_MONITOR_OUTCOME_TOKEN=" + secrets.token_urlsafe(32))
    if not missing:
        return settings
    env_path: Path = ENV_FILE
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_bytes() if env_path.exists() else b""
    suffix = b"" if not existing or existing.endswith(b"\n") else b"\n"
    with env_path.open("ab") as stream:
        stream.write(
            suffix
            + (
                "\n# Local monitoring credentials; do not commit.\n" + "\n".join(missing) + "\n"
            ).encode("utf-8")
        )
    settings = MonitoringSettings()
    settings.require_token()
    return settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a localhost port between 1024 and 65535")
    settings = ensure_monitor_token()
    uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
