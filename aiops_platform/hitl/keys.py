"""Ed25519 signing keys for platform-issued contracts.

Signatures are over Agent 4's canonical JSON (`agent4_deployment_recovery.core.canonical`),
so Agent 4's existing `Ed25519EnvelopeVerifier` checks them unchanged. Private
keys are loaded from PEM files (Kubernetes/compose secrets); `generate()` is for
tests and first-run local setup only.
"""

import base64
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent4_deployment_recovery.adapters.signed_contracts import (
    TrustedIssuerKey,
    TrustedIssuerRegistry,
)
from agent4_deployment_recovery.core.canonical import canonical_json

AGENT4_AUDIENCE = "agent4-deployment-recovery"


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    issuer: str
    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls, key_id: str, issuer: str) -> "SigningKey":
        return cls(key_id, issuer, Ed25519PrivateKey.generate())

    @classmethod
    def from_pem_file(cls, path: str | Path, key_id: str, issuer: str) -> "SigningKey":
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 private key")
        return cls(key_id, issuer, key)

    @classmethod
    def load_or_create(cls, path: str | Path, key_id: str, issuer: str) -> "SigningKey":
        """Local development convenience: create the key file on first start."""
        path = Path(path)
        if path.exists():
            return cls.from_pem_file(path, key_id, issuer)
        key = cls.generate(key_id, issuer)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.private_pem())
        return key

    def private_pem(self) -> bytes:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def public_pem(self) -> str:
        return (
            self.private_key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode("ascii")
        )

    def sign(self, model) -> str:
        """base64url (unpadded) Ed25519 signature over the canonical JSON of `model`."""
        raw = self.private_key.sign(canonical_json(model).encode("utf-8"))
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def trusted_entry(self, audience: str = AGENT4_AUDIENCE) -> TrustedIssuerKey:
        return TrustedIssuerKey(
            key_id=self.key_id, issuer=self.issuer, audience=audience, public_key_pem=self.public_pem()
        )


def registry_for(*keys: SigningKey, audience: str = AGENT4_AUDIENCE) -> TrustedIssuerRegistry:
    """The public-key registry Agent 4 trusts (no private material)."""
    return TrustedIssuerRegistry(keys=tuple(k.trusted_entry(audience) for k in keys))
