"""Agent 6 monitoring settings from its own ignored .env; keys are never logged."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent6_mlops_lifecycle.monitoring.models import MonitorScope
from agent6_mlops_lifecycle.paths import AGENT_DIRECTORY as AGENT_DIRECTORY
from agent6_mlops_lifecycle.paths import ENV_FILE
from agent6_mlops_lifecycle.paths import ROOT as ROOT


class MonitoringSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT6_MONITOR_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )
    benchmark_registry: Path | None = None
    benchmark_python: Path | None = None
    benchmark_database: Path = ROOT / "artifacts/agent6/assurance.sqlite3"
    benchmark_interval_seconds: int = Field(default=3600, ge=60, le=604800)
    benchmark_timeout_seconds: int = Field(default=90, ge=5, le=300)
    advisor_enabled: bool = False
    advisor_model: str = Field(default="openai/gpt-oss-20b", min_length=1, max_length=160)
    advisor_interval_seconds: int = Field(default=900, ge=60, le=86400)
    token: SecretStr = Field(default=SecretStr(""), exclude=True)
    reviewer_id: str = Field(default="local-operator", min_length=1, max_length=160)
    source_database: Path = ROOT / "artifacts/application.sqlite3"
    state_database: Path = ROOT / "artifacts/agent6/state.sqlite3"
    evidence_database: Path = ROOT / "artifacts/agent6/evidence.sqlite3"
    allowed_serving_hosts: tuple[str, ...] = ()
    prediction_url: str = "http://127.0.0.1:8001/predict"
    tenant_id: str = "local-fraud"
    project_id: str = "fraud-detection-serving"
    model_id: str = "fraud-api-pipeline"
    interval_seconds: float = Field(default=5.0, ge=0.2, le=300)
    window_seconds: int = Field(default=3600, ge=10, le=604800)
    min_samples: int = Field(default=30, ge=2, le=20000)
    min_positive_samples: int = Field(default=2, ge=1)
    min_negative_samples: int = Field(default=2, ge=1)
    min_outcome_coverage: float = Field(default=0.5, ge=0, le=1)
    min_quality_score: float = Field(default=0.8, ge=0, le=1)
    min_distribution_samples: int = Field(default=100, ge=2, le=20000)
    psi_threshold: float = Field(default=0.2, gt=0, le=100)
    demo_mode: bool = False
    baseline_mode: Literal["auto", "manual"] = "auto"
    baseline_warmup_seconds: int = Field(default=60, ge=0, le=86400)
    quality_window_seconds: int = Field(default=604800, ge=10, le=2592000)
    min_operational_samples: int = Field(default=10, ge=1, le=20000)
    max_failure_rate: float = Field(default=0.05, ge=0, le=1)
    max_input_issue_rate: float = Field(default=0.05, ge=0, le=1)
    max_latency_p95_ms: float = Field(default=1000, gt=0, allow_inf_nan=False)
    enable_service_probes: bool = True
    application_health_url: str | None = None
    outcome_token: SecretStr = Field(default=SecretStr(""), exclude=True)
    outcome_source_id: str = Field(default="business-outcome-service", min_length=1, max_length=120)
    manual_review_enabled: bool = False
    business_database_url: SecretStr = Field(default=SecretStr(""), exclude=True)
    business_outcome_table: str = "agent6_business_outcomes_v1"
    business_feed_mode: Literal["observation", "transaction"] = "observation"
    business_source_ids: tuple[str, ...] = ("business-service",)
    business_columns: dict[str, str] = Field(default_factory=dict)
    business_batch_size: int = Field(default=100, ge=1, le=1000)

    @field_validator("source_database", "state_database", "evidence_database", "benchmark_database")
    @classmethod
    def absolute_paths(cls, path: Path) -> Path:
        if str(path) == ":memory:":
            raise ValueError("Monitoring databases must be durable")
        return (path if path.is_absolute() else ROOT / path).resolve()

    @model_validator(mode="after")
    def validate_settings(self) -> "MonitoringSettings":
        self.scope()
        import re

        allowed_columns = {
            "sequence_id",
            "event_id",
            "observation_id",
            "model_version",
            "actual",
            "evidence_ref",
            "occurred_at",
            "tenant_id",
            "project_id",
            "model_id",
            "transaction_id",
            "source_id",
            "status",
            "verification_status",
        }
        identifiers = self.business_outcome_table.split(".") + list(self.business_columns.values())
        if (
            len(self.business_outcome_table.split(".")) > 2
            or not set(self.business_columns).issubset(allowed_columns)
            or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in identifiers)
        ):
            raise ValueError("Business table/column mapping contains an invalid SQL identifier")
        url = urlsplit(self.prediction_url)
        if (
            url.scheme not in ("http", "https")
            or url.hostname not in {"127.0.0.1", "localhost", "::1", *self.allowed_serving_hosts}
            or url.username
            or url.password
        ):
            raise ValueError(
                "This local connector requires a loopback prediction API URL without credentials"
            )
        if self.demo_mode and (self.tenant_id != "demo" or not self.project_id.endswith("-demo")):
            raise ValueError(
                "Demo mode requires tenant demo and a separate project ending in -demo"
            )
        if self.application_health_url:
            health_url = urlsplit(self.application_health_url)
            if (
                health_url.scheme not in ("http", "https")
                or health_url.hostname
                not in {"127.0.0.1", "localhost", "::1", *self.allowed_serving_hosts}
                or health_url.username
                or health_url.password
            ):
                raise ValueError("Application health checks require a loopback URL")
        outcome_key = self.outcome_token.get_secret_value()
        if outcome_key and (len(outcome_key) < 32 or outcome_key == self.token.get_secret_value()):
            raise ValueError(
                "Outcome service requires a distinct credential of at least 32 characters"
            )
        if (
            len(
                {
                    self.source_database,
                    self.state_database,
                    self.evidence_database,
                    self.benchmark_database,
                }
            )
            != 4
        ):
            raise ValueError(
                "Source, monitoring state, evidence and benchmark databases must be separate"
            )
        if self.min_positive_samples + self.min_negative_samples > self.min_samples:
            raise ValueError("Class support cannot exceed the minimum labeled sample count")
        return self

    def scope(self) -> MonitorScope:
        return MonitorScope(
            tenant_id=self.tenant_id, project_id=self.project_id, model_id=self.model_id
        )

    def require_token(self) -> None:
        if len(self.token.get_secret_value()) < 32:
            raise ValueError(
                "Configure a monitoring token of at least 32 characters in Agent 6 .env"
            )
