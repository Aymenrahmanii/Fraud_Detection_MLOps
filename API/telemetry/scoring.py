"""Probability evidence for trusted local binary models, never inferred from a class."""

import logging
import math
from numbers import Real
from typing import Any


def probability_evidence(
    model: Any, frame: Any, predicted: int
) -> tuple[float | None, float | None]:
    """Keep authoritative inference available for models without a probability adapter."""
    try:
        native = model.unwrap_python_model()
        values = native.predict_proba(frame)
        threshold = (
            native.best_threshold
            if hasattr(native, "best_threshold")
            else native.threshold
        )
        if (
            len(values) != 1
            or not isinstance(values[0], Real)
            or not isinstance(threshold, Real)
        ):
            return None, None
        probability, threshold = float(values[0]), float(threshold)
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (probability, threshold)):
            return None, None
        if predicted != int(probability >= threshold):
            return None, None
        return probability, threshold
    except Exception as error:  # noqa: BLE001 -- optional adapter failure must not fail inference.
        logging.getLogger(__name__).warning(
            "Probability adapter unavailable (%s)", type(error).__name__
        )
        return None, None
