"""Versioned metric implementations registered by trusted application code."""

from collections.abc import Callable
from dataclasses import dataclass

from agent6_mlops_lifecycle.contracts.mlops_metric_policy import (
    MetricDefinition,
    MetricEvaluationPolicy,
    MetricValue,
)
from agent6_mlops_lifecycle.contracts.mlops_projects import (
    BinaryEvaluationSample,
    EvaluationBatch,
    TaskType,
)
from agent6_mlops_lifecycle.core.errors import IntegrationNotConfiguredError

type MetricFunction = Callable[[EvaluationBatch], MetricValue]


@dataclass(frozen=True)
class RegisteredMetric:
    definition: MetricDefinition
    function: MetricFunction


class MetricRegistry:
    """No dynamic imports, metric guessing, replacement or execution from configuration."""

    def __init__(self) -> None:
        self._metrics: dict[str, RegisteredMetric] = {}

    def register(self, definition: MetricDefinition, function: MetricFunction) -> None:
        value = MetricDefinition.model_validate_json(definition.model_dump_json())
        if not callable(function):
            raise ValueError("Metric implementation must be a trusted callable")
        if value.metric_id in self._metrics:
            raise ValueError("A versioned metric implementation is already registered")
        self._metrics[value.metric_id] = RegisteredMetric(value, function)

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return tuple(self._metrics[key].definition for key in sorted(self._metrics))

    def validate_policy(self, task_type: TaskType, policy: MetricEvaluationPolicy) -> None:
        for gate in policy.gates:
            registered = self._metrics.get(gate.metric_id)
            if registered is None:
                raise IntegrationNotConfiguredError(f"Metric is not registered: {gate.metric_id}")
            definition = registered.definition
            if task_type not in definition.task_types:
                raise ValueError(f"Metric {gate.metric_id} does not support task {task_type}")
            if gate.operator != definition.direction:
                raise ValueError(
                    f"Metric {gate.metric_id} requires operator {definition.direction}"
                )
            for value in (gate.threshold, gate.baseline.value if gate.baseline else None):
                if value is not None:
                    self._check_domain(definition, value)

    @staticmethod
    def _check_domain(definition: MetricDefinition, value: float) -> None:
        if (definition.minimum is not None and value < definition.minimum) or (
            definition.maximum is not None and value > definition.maximum
        ):
            raise ValueError(f"Value is outside the domain of {definition.metric_id}")

    def compute(self, metric_id: str, batch: EvaluationBatch) -> MetricValue:
        registered = self._metrics.get(metric_id)
        if registered is None:
            raise IntegrationNotConfiguredError(f"Metric is not registered: {metric_id}")
        definition = registered.definition
        if any(sample.task_type not in definition.task_types for sample in batch.samples):
            raise ValueError("Metric received samples for an incompatible task type")
        if definition.requires_positive_probability and any(
            not isinstance(sample, BinaryEvaluationSample) or sample.positive_probability is None
            for sample in batch.samples
        ):
            return MetricValue(unavailable_reason="Positive-class probabilities are missing")
        # Isolate mutable custom payload dictionaries between metric callbacks.
        metric_input = EvaluationBatch.model_validate_json(batch.model_dump_json())
        value = registered.function(metric_input)
        # Trusted plugins still must return a valid, finite contract in the declared domain.
        value = MetricValue.model_validate_json(value.model_dump_json())
        if value.value is not None:
            self._check_domain(definition, value.value)
        return value
