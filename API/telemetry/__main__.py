# ruff: noqa: N999  # The existing application package is named API.
"""Export a local serving-prediction window for Agent 6 without running inference."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from API.telemetry.config import TelemetryConfig
from API.telemetry.export import export_predictions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export",))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--lookback-minutes", type=int, default=60)
    parser.add_argument("--window-start", type=datetime.fromisoformat)
    parser.add_argument("--window-end", type=datetime.fromisoformat)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--model-version")
    args = parser.parse_args()
    try:
        config = TelemetryConfig.from_env()
        now = datetime.now(UTC)
        if args.lookback_minutes <= 0:
            raise ValueError("--lookback-minutes must be positive")
        if (args.window_start is None) != (args.window_end is None):
            raise ValueError("Set both --window-start and --window-end")
        end = args.window_end or now
        start = args.window_start or end - timedelta(minutes=args.lookback_minutes)
        result = export_predictions(
            database=args.database or config.database,
            output_dir=args.output_dir,
            scope=config.scope,
            window_start=start,
            window_end=end,
            as_of=args.as_of or now,
            model_version=args.model_version,
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
