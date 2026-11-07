"""Trusted saved-model inference in the project's compatible Python runtime.

No serving endpoint is called and no live transaction or outcome is inserted.
"""

import contextlib
import importlib
import json
import sqlite3
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any


def artifact_digest(path: Path) -> str:
    if path.is_file():
        return sha256(path.read_bytes()).hexdigest()
    digest = sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file())
    if not files:
        raise ValueError("Empty artifact")
    for file in files:
        if not file.resolve().is_relative_to(path.resolve()):
            raise ValueError("Artifact symlink escapes the configured directory")
        digest.update(file.relative_to(path).as_posix().encode())
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def score(request: dict[str, Any]) -> dict[str, Any]:
    # This worker accepts only operator-configured local artifacts, never user uploads.
    project = Path(request["project_root"]).resolve(strict=True)
    artifact = Path(request["artifact"]).resolve(strict=True)
    if not artifact.is_relative_to(project):
        raise ValueError("Artifact is outside the configured project")
    actual_digest = artifact_digest(artifact)
    if actual_digest != request["artifact_sha256"]:
        raise ValueError("Artifact changed before inference")
    database = Path(request["database"]).resolve(strict=True)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != 0x41364153:
            raise ValueError("Unsupported benchmark database")
        row = connection.execute(
            "SELECT payload FROM documents WHERE id=? AND kind='dataset'", (request["dataset_id"],)
        ).fetchone()
    finally:
        connection.close()
    if (
        row is None
        or len(row[0]) > 16_000_000
        or sha256(row[0]).hexdigest() != request["dataset_id"]
    ):
        raise ValueError("Benchmark digest validation failed")
    dataset = json.loads(row[0])
    rows = dataset["rows"]
    if not 2 <= len(rows) <= 20000:
        raise ValueError("Invalid benchmark sample count")
    sys.path.insert(0, str(project))
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import pandas as pd

        frame = pd.DataFrame([row["inputs"] for row in rows])
        frame = frame.where(pd.notnull(frame), np.nan)
        if request["kind"] == "mlflow":
            import mlflow.pyfunc

            model = mlflow.pyfunc.load_model(str(artifact))
            predictions = np.asarray(model.predict(frame))
        elif request["kind"] == "joblib":
            joblib = importlib.import_module("joblib")

            # Existing project serialization needs its training classes importable.
            importlib.import_module("Src.model")

            model = joblib.load(artifact)
            probabilities = np.asarray(model.predict_proba(frame))
            predictions = (probabilities >= model.best_threshold).astype(int)
        else:
            raise ValueError("Unsupported configured artifact kind")
    if (
        predictions.ndim != 1
        or len(predictions) != len(rows)
        or not np.isin(predictions, [0, 1]).all()
    ):
        raise ValueError("Model did not return aligned binary predictions")
    if artifact_digest(artifact) != actual_digest:
        raise ValueError("Artifact changed while evaluating")
    return {
        "artifact_sha256": actual_digest,
        "dataset_id": request["dataset_id"],
        "predictions": [int(value) for value in predictions],
    }


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read(32769))
        print(json.dumps(score(request), allow_nan=False))
    except Exception:
        # No request values, credentials or model exception text in the parent logs.
        print(
            "Benchmark worker failed; inspect artifact compatibility and catalog integrity",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
