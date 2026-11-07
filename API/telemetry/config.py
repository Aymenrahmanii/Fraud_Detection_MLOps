"""Producer-local environment configuration; no dependence on Agent 6's .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from API.telemetry.contracts import Scope

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TelemetryConfig:
    database: Path
    scope: Scope
    outcome_token: str = field(default="", repr=False)
    business_source_ids: tuple[str, ...] = ("business-service",)

    @classmethod
    def from_env(cls) -> "TelemetryConfig":
        file_values = dotenv_values(Path(__file__).resolve().parent / ".env")

        def setting(name: str, default: str) -> str:
            value = os.environ.get(name, file_values.get(name, default))
            if value is None or not value.strip():
                raise ValueError(f"{name} must be nonempty")
            return value.strip()

        database = Path(
            setting("AGENT6_PREDICTION_DATABASE", "artifacts/application.sqlite3")
        )
        if str(database) == ":memory:":
            raise ValueError("Prediction logging requires a durable database")
        if not database.is_absolute():
            database = ROOT / database
        return cls(
            database.resolve(),
            Scope(
                tenant_id=setting("AGENT6_TENANT_ID", "local-fraud"),
                project_id=setting("AGENT6_PROJECT_ID", "fraud-detection-serving"),
                model_id=setting("AGENT6_MODEL_ID", "fraud-api-pipeline"),
            ),
            str(
                os.environ.get(
                    "AGENT6_OUTCOME_SERVICE_TOKEN",
                    file_values.get("AGENT6_OUTCOME_SERVICE_TOKEN") or "",
                )
            ).strip(),
            tuple(
                value.strip()
                for value in setting(
                    "AGENT6_BUSINESS_SOURCE_IDS", "business-service"
                ).split(",")
                if value.strip()
            ),
        )
