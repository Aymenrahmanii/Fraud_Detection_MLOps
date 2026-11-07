"""Autonomous routine work; review only for consequential or insufficiently evidenced changes."""

import sys
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent6_mlops_lifecycle.paths import ENV_FILE, ROOT


class LifecycleSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT6_LIFECYCLE_", env_file=ENV_FILE, extra="ignore"
    )
    integration_token: SecretStr = Field(default=SecretStr(""), exclude=True)
    enabled: bool = True
    allow_verification_jobs: bool = False
    interval_seconds: int = Field(default=30, ge=1, le=3600)
    model_python: Path = Path(sys.executable)
    training_data: Path = ROOT / "Data/payment_fraud.csv"
    min_verified_labels: int = Field(default=100, ge=30, le=1000)
    min_class_samples: int = Field(default=20, ge=5)
    min_guard_labels: int = Field(default=50, ge=30, le=500)
    min_guard_class_samples: int = Field(default=5, ge=5, le=100)
    cooldown_hours: int = Field(default=24, ge=1, le=720)
    max_jobs_per_day: int = Field(default=2, ge=1, le=20)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    min_f1: float = Field(default=0.85, ge=0, le=1)
    min_recall: float = Field(default=0.9, ge=0, le=1)
    max_false_positive_rate: float = Field(default=0.05, ge=0, le=1)
    min_f1_improvement: float = Field(default=0.005, ge=0, le=1)
    max_recall_drop: float = Field(default=0.01, ge=0, le=1)
    auto_promote: bool = False
    policy_calibrated: bool = False
    review_expiry_hours: int = Field(default=48, ge=1, le=168)

    @model_validator(mode="after")
    def validate_policy(self) -> "LifecycleSettings":
        if self.min_class_samples * 2 > self.min_verified_labels:
            raise ValueError("Class support exceeds the minimum labeled sample count")
        if self.auto_promote and not self.policy_calibrated:
            raise ValueError("Automatic promotion requires an explicitly calibrated policy")
        return self
