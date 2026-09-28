"""Fixtures for the API test suite.

API/services.py loads an MLflow model at *import time* (a design smell in
its own right -- import-time side effects make a module impossible to
test without a working model registry, and crash the whole process on
startup if the registry is unavailable, instead of failing one request).

Separately, and more seriously: the committed local MLflow registry under
API/mlruns is not portable. Its "champion"-aliased model version's
storage_location is a hardcoded absolute path from the original author's
own machine (file:///c:/Users/Asus/Downloads/...), so mlflow.pyfunc.
load_model() fails on any other machine, including this one. That is a
real, separate bug -- not something this stub silently fixes.

Until both are addressed, we replace `mlflow` in sys.modules with a
stub *before* API.main (and therefore API.services) is ever imported, so
the API test suite exercises the FastAPI layer without depending on the
broken registry. Tests that care about a specific prediction value
monkeypatch `API.services.model.predict` directly.
"""

import sys
from unittest.mock import MagicMock

import numpy as np


def _install_mlflow_stub() -> None:
    if "API.services" in sys.modules or "API.main" in sys.modules:
        # Something already imported the real thing this session -- too
        # late to stub, and it would be misleading to pretend otherwise.
        return

    fake_model_info = MagicMock()
    fake_model_info.tags = {}

    fake_client = MagicMock()
    fake_client.get_model_version_by_alias.return_value = fake_model_info

    fake_loaded_model = MagicMock()
    fake_loaded_model.predict.return_value = np.array([0])

    mlflow_module = MagicMock(name="mlflow_stub")
    mlflow_module.set_tracking_uri = MagicMock()
    mlflow_module.MlflowClient = MagicMock(return_value=fake_client)
    mlflow_module.pyfunc.load_model = MagicMock(return_value=fake_loaded_model)

    sys.modules["mlflow"] = mlflow_module
    sys.modules["mlflow.pyfunc"] = mlflow_module.pyfunc


_install_mlflow_stub()
