"""Private, operator-controlled assembly for Agent 4 read-only services.

This module deliberately does not assemble a live mutation runtime. External
callers receive the resulting read-only object and cannot provide verifiers,
keys, Kubernetes clients, allowlists, or permits.
"""

import os
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import model_validator

from agent4_deployment_recovery.adapters.kubernetes import OfficialKubernetesAdapter
from agent4_deployment_recovery.adapters.signed_contracts import (
    TrustedIssuerKey,
    TrustedIssuerRegistry,
)
from agent4_deployment_recovery.core.kubernetes_models import (
    AllowedDeploymentTarget,
    KubernetesConnectionConfig,
    PreflightPolicy,
)
from agent4_deployment_recovery.core.schemas import Identifier, NonEmptyStr, StrictModel
from agent4_deployment_recovery.mcp_server import (
    ReadOnlyMCPService,
    create_mcp_server,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal

OPERATOR_CONFIG_ENV = "AGENT4_OPERATOR_CONFIG_PATH"


class TrustedIssuerKeyFile(StrictModel):
    key_id: Identifier
    issuer: Identifier
    audience: NonEmptyStr
    public_key_path: Path


class OperatorRuntimeConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["READ_ONLY", "FUTURE_LIVE"] = "READ_ONLY"
    journal_path: Path
    inspection_enabled: bool = False
    expose_execution_data: Literal[False] = False
    kubernetes: KubernetesConnectionConfig | None = None
    allowed_targets: tuple[AllowedDeploymentTarget, ...] = ()
    allowed_live_environments: frozenset[Identifier] = frozenset()
    authorization_keys: tuple[TrustedIssuerKeyFile, ...] = ()
    agent1_keys: tuple[TrustedIssuerKeyFile, ...] = ()
    authorization_audience: NonEmptyStr = "agent4-deployment-recovery"
    agent1_audience: NonEmptyStr = "agent4-deployment-recovery"

    @model_validator(mode="after")
    def validate_mode(self):
        if not self.journal_path.is_absolute():
            raise ValueError("operator journal_path must be absolute")
        if self.inspection_enabled and self.kubernetes is None:
            raise ValueError("inspection requires explicit Kubernetes configuration")
        if self.kubernetes is not None:
            if (
                self.kubernetes.kubeconfig_path is not None
                and (
                    not self.kubernetes.kubeconfig_path.is_absolute()
                    or not self.kubernetes.kubeconfig_path.is_file()
                )
            ):
                raise ValueError(
                    "operator kubeconfig_path must be absolute and exist"
                )
            clusters = {item.cluster_id for item in self.allowed_targets}
            if clusters and clusters != {self.kubernetes.cluster_id}:
                raise ValueError("target allowlist does not match configured cluster identity")
        if self.mode == "FUTURE_LIVE" and (
            self.kubernetes is None
            or not self.allowed_targets
            or not self.allowed_live_environments
            or not self.authorization_keys
            or not self.agent1_keys
        ):
            raise ValueError("future live configuration is incomplete")
        return self


class ReadOnlyRuntime:
    """Assembled dependencies exposed to read-only interfaces only."""

    def __init__(self, *, config: OperatorRuntimeConfig):
        self.config = config
        self.journal = SQLiteExecutionJournal(config.journal_path)
        self.policy = (
            PreflightPolicy(allowed_targets=config.allowed_targets)
            if config.allowed_targets
            else None
        )
        self.inspector = (
            OfficialKubernetesAdapter.from_config(config.kubernetes)
            if config.inspection_enabled and config.kubernetes is not None
            else None
        )
        self.live_execution_available = False


def _load_key_registry(
    specifications: tuple[TrustedIssuerKeyFile, ...],
    *,
    expected_audience: str,
) -> TrustedIssuerRegistry | None:
    if not specifications:
        return None
    keys = []
    for item in specifications:
        if item.audience != expected_audience:
            raise ValueError("trusted issuer key audience does not match runtime audience")
        if not item.public_key_path.is_absolute() or not item.public_key_path.is_file():
            raise ValueError("trusted public-key paths must be absolute existing files")
        public_key_pem = item.public_key_path.read_text(encoding="utf-8")
        try:
            public_key = serialization.load_pem_public_key(public_key_pem.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("trusted public key is malformed") from exc
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("trusted public key must be Ed25519")
        keys.append(
            TrustedIssuerKey(
                key_id=item.key_id,
                issuer=item.issuer,
                audience=item.audience,
                public_key_pem=public_key_pem,
            )
        )
    return TrustedIssuerRegistry(keys=tuple(keys))


def load_operator_runtime_config() -> OperatorRuntimeConfig:
    """Load only the fixed operator process environment; accepts no caller input."""
    raw_path = os.environ.get(OPERATOR_CONFIG_ENV)
    if raw_path is None:
        raise ValueError(f"{OPERATOR_CONFIG_ENV} is not configured")
    path = Path(raw_path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("operator configuration path must be absolute and exist")
    try:
        payload_json = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("operator configuration cannot be read") from exc
    return OperatorRuntimeConfig.model_validate_json(payload_json)


def assemble_read_only_runtime() -> ReadOnlyRuntime:
    """Validate trust material, then assemble no live mutation capability."""
    config = load_operator_runtime_config()
    _load_key_registry(
        config.authorization_keys,
        expected_audience=config.authorization_audience,
    )
    _load_key_registry(config.agent1_keys, expected_audience=config.agent1_audience)
    return ReadOnlyRuntime(config=config)


def assemble_operator_mcp_server():
    """Assemble the MCP server from operator configuration with no caller injection."""
    runtime = assemble_read_only_runtime()
    service = ReadOnlyMCPService(
        inspector=runtime.inspector,
        policy=runtime.policy,
    )
    return create_mcp_server(service)
