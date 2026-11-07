"""Object storage for large immutable evidence (v5 section 6.2: MinIO/S3).

Evidence bodies are content-addressed: the object key is the SHA-256 of the
canonical JSON, so writing the same evidence twice is a no-op. The local store
keeps files under a directory; an S3/MinIO store can implement the same two
methods.
"""

import hashlib
import json
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue


def canonical_bytes(content: JsonValue) -> bytes:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore(Protocol):
    def put(self, incident_id: str, data: bytes) -> tuple[str, str]:
        """Store bytes; return (object_uri, sha256)."""

    def get(self, object_uri: str) -> bytes: ...


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def put(self, incident_id: str, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        safe_incident = "".join(c if c.isalnum() or c in "-_" else "_" for c in incident_id)
        path = self._root / safe_incident / f"{digest}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return f"evidence://{safe_incident}/{digest}", digest

    def get(self, object_uri: str) -> bytes:
        prefix = "evidence://"
        if not object_uri.startswith(prefix):
            raise ValueError(f"not a local evidence URI: {object_uri}")
        incident, digest = object_uri[len(prefix) :].split("/", 1)
        path = (self._root / incident / f"{digest}.json").resolve()
        if self._root.resolve() not in path.parents:
            raise ValueError("evidence URI escapes the object store")
        data = path.read_bytes()
        if sha256_hex(data) != digest:
            raise ValueError("evidence object is corrupted (checksum mismatch)")
        return data


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, incident_id: str, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        uri = f"evidence://{incident_id}/{digest}"
        self._objects.setdefault(uri, data)
        return uri, digest

    def get(self, object_uri: str) -> bytes:
        return self._objects[object_uri]
