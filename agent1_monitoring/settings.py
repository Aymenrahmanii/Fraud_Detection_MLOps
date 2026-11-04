"""Environment-driven configuration of Agent 1. Secrets stay out of logs."""

from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIDS_", env_file=Path(__file__).with_name(".env"), extra="ignore"
    )

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    # Optional: without a key, unknown log patterns simply stay UNKNOWN.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def blank_key_is_absent(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value
