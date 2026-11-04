import os
from pathlib import Path

import mlflow.pyfunc
import numpy as np
import pandas as pd

from API.telemetry.identity import artifact_sha256

# Loaded directly from its artifact directory rather than through MLflow's
# local Model Registry (models:/FraudDetectionPipeline@champion). The
# registry's own metadata bakes an ABSOLUTE artifact path in at
# registration time -- every registered version here points at
# c:/Users/Asus/Downloads/Fraud_MLOps_Project/... on the original
# training machine, which cannot resolve on any other machine, in CI, or
# in a container. That isn't a one-off typo: it's inherent to how
# MLflow's file-based store works, so re-pointing it at this machine's
# path would only break again on the next machine. The actual artifact
# files are checked into the repo at a stable, portable relative path,
# so we load from there directly instead.
MODEL_ARTIFACT_PATH = os.environ.get(
    "MODEL_ARTIFACT_PATH",
    "API/mlruns/369953768913727304/models/m-f8ae82f65eaf4a12b5d02dce9939bf52/artifacts",
)

_root = Path(__file__).resolve().parents[1]
_artifact = Path(MODEL_ARTIFACT_PATH)
if not _artifact.is_absolute():
    _artifact = _root / _artifact
MODEL_ARTIFACT_PATH = str(_artifact.resolve(strict=True))
MODEL_ARTIFACT_SHA256 = artifact_sha256(Path(MODEL_ARTIFACT_PATH))
model = mlflow.pyfunc.load_model(MODEL_ARTIFACT_PATH)
if artifact_sha256(Path(MODEL_ARTIFACT_PATH)) != MODEL_ARTIFACT_SHA256:
    raise RuntimeError("Serving artifact changed while loading the model")


def predict_fraud_with_evidence(data: dict):
    df = pd.DataFrame([data])
    df = df.where(pd.notnull(df), np.nan)  # converts None to NaN
    preds = model.predict(df)
    if len(preds) != 1 or preds[0] not in (0, 1):
        raise ValueError("Serving model must return one binary class")
    from API.telemetry.scoring import probability_evidence

    predicted = int(preds[0])
    probability, threshold = probability_evidence(model, df, predicted)
    return predicted, probability, threshold


def predict_fraud(data: dict):
    return predict_fraud_with_evidence(data)[0]
