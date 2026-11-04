"""Saved candidate with a threshold selected only on the validation split."""

from typing import Any

import numpy as np
import pandas as pd
from mlflow.pyfunc.model import PythonModel
from numpy.typing import NDArray


class ThresholdModel(PythonModel):
    def __init__(self, pipeline: Any, threshold: float) -> None:
        self.pipeline, self.threshold = pipeline, threshold

    def predict(
        self, context: Any, model_input: pd.DataFrame, params: dict[str, Any] | None = None
    ) -> NDArray[np.int64]:
        return np.asarray(self.predict_proba(model_input) >= self.threshold, dtype=np.int64)

    def predict_proba(self, model_input: pd.DataFrame) -> NDArray[np.float64]:
        return np.asarray(self.pipeline.predict_proba(model_input)[:, 1], dtype=np.float64)
