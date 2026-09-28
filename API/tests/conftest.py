"""Fixtures for the API test suite.

API/services.py loads its MLflow model at *import time*. As of the fix
in this same change, it loads directly from the artifact's checked-in
relative path rather than through the local MLflow Model Registry, so it
actually works on any machine now (the registry itself baked an absolute
path from the original training machine and could not resolve
elsewhere -- see the comment in services.py for the full story).

Even though loading the real model now works, we still stub `mlflow`
here before API.main (and therefore API.services) is imported, for
reasons independent of that bug:
  - Speed/determinism: the real model requires exact pinned library
    versions (scikit-learn==1.6.1, etc.) to unpickle without warnings,
    which the API test suite shouldn't have to depend on.
  - The import-time loading itself is still a design smell (any load
    failure crashes the whole process on startup rather than failing
    one request) -- unrelated to this fix, not addressed here.

Tests that care about a specific prediction value monkeypatch
`API.services.model.predict` directly.
"""

import sys
from unittest.mock import MagicMock

import numpy as np


def _install_mlflow_stub() -> None:
    if "API.services" in sys.modules or "API.main" in sys.modules:
        # Something already imported the real thing this session -- too
        # late to stub, and it would be misleading to pretend otherwise.
        return

    fake_loaded_model = MagicMock()
    fake_loaded_model.predict.return_value = np.array([0])

    mlflow_module = MagicMock(name="mlflow_stub")
    mlflow_module.pyfunc.load_model = MagicMock(return_value=fake_loaded_model)

    sys.modules["mlflow"] = mlflow_module
    sys.modules["mlflow.pyfunc"] = mlflow_module.pyfunc


_install_mlflow_stub()
