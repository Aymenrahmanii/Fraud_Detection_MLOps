"""Fingerprint the exact local serving artifact before and after model loading."""

from hashlib import sha256
from pathlib import Path


def artifact_sha256(directory: Path) -> str:
    directory = directory.resolve(strict=True)
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        raise ValueError("The serving artifact directory is empty")
    digest = sha256()
    for path in files:
        if not path.resolve().is_relative_to(directory):
            raise ValueError("Model artifact links cannot escape their directory")
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
