"""Service identity on gRPC calls (v5 sections 4.1 and 18 "Service identity").

Each service holds one credential: its identity (`role:name`) and an HMAC
token over it. Only the serving side holds the secret, so a service cannot mint
another service's token. Production replaces this with mTLS workload identity
(`grpc.ssl_channel_credentials` with per-service certificates); the servers
already accept TLS credentials when certificate paths are configured.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass

from aiops_platform.context.service import Principal, Role

H_PRINCIPAL = "x-aiops-principal"
H_TOKEN = "x-aiops-token"


class Unauthenticated(PermissionError):
    pass


def issue_token(secret: str, principal: Principal) -> str:
    """Run by the operator at deploy time: one token per service identity."""
    material = f"{principal.role.value}:{principal.name}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Credential:
    principal: Principal
    token: str

    def metadata(self) -> tuple[tuple[str, str], ...]:
        return ((H_PRINCIPAL, f"{self.principal.role.value}:{self.principal.name}"), (H_TOKEN, self.token))

    @classmethod
    def from_env(cls, role: Role, name: str) -> "Credential":
        token = os.environ.get("AIOPS_SERVICE_TOKEN")
        if not token:
            secret = os.environ.get("AIOPS_SERVICE_SECRET")
            if not secret:
                raise RuntimeError("AIOPS_SERVICE_TOKEN (or AIOPS_SERVICE_SECRET in development) is required")
            token = issue_token(secret, Principal(name, role))
        return cls(Principal(name, role), token)


class Verifier:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def principal(self, metadata) -> Principal:
        values = {key: value for key, value in (metadata or ())}
        raw, token = values.get(H_PRINCIPAL), values.get(H_TOKEN)
        if not raw or not token or ":" not in raw:
            raise Unauthenticated("missing service identity")
        role, name = raw.split(":", 1)
        try:
            principal = Principal(name, Role(role))
        except ValueError as exc:
            raise Unauthenticated(f"unknown role {role}") from exc
        if not hmac.compare_digest(issue_token(self._secret, principal), token):
            raise Unauthenticated("invalid service token")
        return principal
