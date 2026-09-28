import os

import mlflow.pyfunc
import numpy as np
import pandas as pd

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

model = mlflow.pyfunc.load_model(MODEL_ARTIFACT_PATH)


def predict_fraud(data: dict):
    df = pd.DataFrame([data])
    df = df.where(pd.notnull(df), np.nan)  # converts None to NaN
    preds = model.predict(df)
    return int(preds[0])
