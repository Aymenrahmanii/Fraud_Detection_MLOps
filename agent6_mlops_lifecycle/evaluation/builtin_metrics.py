"""Deterministic binary and regression metrics; undefined values stay unavailable."""

from math import fsum, isfinite, log, log1p, sqrt
from sys import float_info
from typing import cast

from agent6_mlops_lifecycle.contracts.mlops_metric_policy import MetricDefinition, MetricValue
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    BinaryEvaluationSample,
    EvaluationBatch,
    RegressionEvaluationSample,
)
from agent6_mlops_lifecycle.evaluation.metric_registry import MetricRegistry


def _binary(batch: EvaluationBatch) -> tuple[BinaryEvaluationSample, ...]:
    if any(not isinstance(row, BinaryEvaluationSample) for row in batch.samples):
        raise ValueError("Binary metric requires binary evaluation samples")
    return cast(tuple[BinaryEvaluationSample, ...], batch.samples)


def _counts(batch: EvaluationBatch) -> tuple[int, int, int, int]:
    rows = _binary(batch)
    tp = sum(row.expected == 1 and row.predicted == 1 for row in rows)
    fp = sum(row.expected == 0 and row.predicted == 1 for row in rows)
    fn = sum(row.expected == 1 and row.predicted == 0 for row in rows)
    tn = len(rows) - tp - fp - fn
    return tp, fp, fn, tn


def _ratio(numerator: float, denominator: float, reason: str) -> MetricValue:
    return (
        MetricValue(value=numerator / denominator)
        if denominator
        else MetricValue(unavailable_reason=reason)
    )


def accuracy(batch: EvaluationBatch) -> MetricValue:
    tp, fp, fn, tn = _counts(batch)
    return _ratio(tp + tn, tp + fp + fn + tn, "No labeled samples")


def precision(batch: EvaluationBatch) -> MetricValue:
    tp, fp, _, _ = _counts(batch)
    return _ratio(tp, tp + fp, "Precision is undefined without predicted positives")


def recall(batch: EvaluationBatch) -> MetricValue:
    tp, _, fn, _ = _counts(batch)
    return _ratio(tp, tp + fn, "Recall is undefined without positive reference labels")


def f1(batch: EvaluationBatch) -> MetricValue:
    tp, fp, fn, _ = _counts(batch)
    return _ratio(
        2 * tp, 2 * tp + fp + fn, "F1 is undefined without positive labels or predictions"
    )


def specificity(batch: EvaluationBatch) -> MetricValue:
    _, fp, _, tn = _counts(batch)
    return _ratio(tn, tn + fp, "Specificity is undefined without negative reference labels")


def balanced_accuracy(batch: EvaluationBatch) -> MetricValue:
    tp, fp, fn, tn = _counts(batch)
    if not tp + fn or not tn + fp:
        return MetricValue(unavailable_reason="Balanced accuracy requires both reference classes")
    return MetricValue(value=0.5 * (tp / (tp + fn) + tn / (tn + fp)))


def _probabilities(batch: EvaluationBatch) -> list[tuple[int, float]]:
    rows = _binary(batch)
    if any(row.positive_probability is None for row in rows):
        raise ValueError("Positive-class probabilities are missing")
    return [(row.expected, cast(float, row.positive_probability)) for row in rows]


def brier(batch: EvaluationBatch) -> MetricValue:
    rows = _probabilities(batch)
    return MetricValue(value=fsum((p - label) ** 2 for label, p in rows) / len(rows))


def log_loss(batch: EvaluationBatch) -> MetricValue:
    # The versioned definition pins float64 epsilon, matching standard finite log-loss practice.
    rows = _probabilities(batch)
    losses = []
    for label, probability in rows:
        clipped = min(1 - float_info.epsilon, max(float_info.epsilon, probability))
        losses.append(-log(clipped) if label else -log1p(-clipped))
    return MetricValue(value=fsum(losses) / len(rows))


def roc_auc(batch: EvaluationBatch) -> MetricValue:
    rows = sorted(_probabilities(batch), key=lambda row: row[1])
    positives = sum(label for label, _ in rows)
    negatives = len(rows) - positives
    if not positives or not negatives:
        return MetricValue(unavailable_reason="ROC AUC requires both reference classes")
    positive_ranks = 0.0
    index = 0
    while index < len(rows):
        end = index + 1
        while end < len(rows) and rows[end][1] == rows[index][1]:
            end += 1
        mean_rank = (index + 1 + end) / 2
        positive_ranks += mean_rank * sum(label for label, _ in rows[index:end])
        index = end
    return MetricValue(
        value=(positive_ranks - positives * (positives + 1) / 2) / (positives * negatives)
    )


def _regression(batch: EvaluationBatch) -> tuple[RegressionEvaluationSample, ...]:
    if any(not isinstance(row, RegressionEvaluationSample) for row in batch.samples):
        raise ValueError("Regression metric requires regression samples")
    return cast(tuple[RegressionEvaluationSample, ...], batch.samples)


def _regression_value(batch: EvaluationBatch, kind: str) -> MetricValue:
    rows = _regression(batch)
    try:
        errors = [row.predicted - row.expected for row in rows]
        if any(not isfinite(error) for error in errors):
            return MetricValue(unavailable_reason="Regression error overflow")
        if kind == "mae":
            value = fsum(abs(error) / len(rows) for error in errors)
        elif kind == "rmse":
            scale = max(abs(error) for error in errors)
            value = (
                scale * sqrt(fsum((error / scale) ** 2 for error in errors) / len(rows))
                if scale
                else 0.0
            )
        elif kind == "mse":
            value = fsum(error * error / len(rows) for error in errors)
        else:
            mean = fsum(row.expected / len(rows) for row in rows)
            deviations = [row.expected - mean for row in rows]
            scale = max(abs(value) for value in (*errors, *deviations))
            if not scale:
                return MetricValue(
                    unavailable_reason="R2 is undefined for constant reference values"
                )
            residual = fsum((error / scale) ** 2 for error in errors)
            total = fsum((deviation / scale) ** 2 for deviation in deviations)
            if not total:
                return MetricValue(
                    unavailable_reason="R2 is undefined for constant reference values"
                )
            if len(rows) < 2:
                return MetricValue(unavailable_reason="R2 requires at least two samples")
            value = 1 - residual / total
        if not isfinite(value):
            return MetricValue(unavailable_reason="Regression metric overflow")
        return MetricValue(value=value)
    except (OverflowError, ZeroDivisionError, ValueError):
        return MetricValue(unavailable_reason="Regression metric is numerically unavailable")


def mae(batch: EvaluationBatch) -> MetricValue:
    return _regression_value(batch, "mae")


def rmse(batch: EvaluationBatch) -> MetricValue:
    return _regression_value(batch, "rmse")


def mse(batch: EvaluationBatch) -> MetricValue:
    return _regression_value(batch, "mse")


def r2(batch: EvaluationBatch) -> MetricValue:
    return _regression_value(batch, "r2")


def default_metric_registry() -> MetricRegistry:
    registry = MetricRegistry()
    for name, function, needs_probability in (
        ("accuracy", accuracy, False),
        ("precision", precision, False),
        ("recall", recall, False),
        ("f1", f1, False),
        ("specificity", specificity, False),
        ("balanced_accuracy", balanced_accuracy, False),
        ("brier", brier, True),
        ("log_loss", log_loss, True),
        ("roc_auc", roc_auc, True),
    ):
        loss = name in ("brier", "log_loss")
        registry.register(
            MetricDefinition(
                metric_id=f"binary.{name}.v1",
                task_types=("binary_classification",),
                direction="lte" if loss else "gte",
                description=f"Binary {name}",
                units="float64-clipped cross entropy" if name == "log_loss" else "ratio",
                minimum=0,
                maximum=None if name == "log_loss" else 1,
                requires_positive_probability=needs_probability,
            ),
            function,
        )
    for name, function in (("mae", mae), ("rmse", rmse), ("mse", mse), ("r2", r2)):
        registry.register(
            MetricDefinition(
                metric_id=f"regression.{name}.v1",
                task_types=("regression",),
                direction="gte" if name == "r2" else "lte",
                description=f"Regression {name}",
                units="dimensionless"
                if name == "r2"
                else "target squared units"
                if name == "mse"
                else "target units",
                minimum=None if name == "r2" else 0,
                maximum=1 if name == "r2" else None,
            ),
            function,
        )
    return registry
