"""Scoped fraud assessment over independently confirmed live observations."""

import json
from hashlib import sha256

from API.telemetry.business import feature_json
from API.telemetry.operations import bucket_features

from agent6_mlops_lifecycle.fraud.metrics import confusion, probability_quality
from agent6_mlops_lifecycle.fraud.models import FraudEvaluation, FraudSettings, FraudSlice
from agent6_mlops_lifecycle.monitoring.source import (
    DirectObservationSource,
    SourceFrame,
    SourceUnavailableError,
)


def assess_fraud(
    source: DirectObservationSource, frame: SourceFrame, policy: FraudSettings
) -> FraudEvaluation:
    predictions = frame.snapshot.predictions
    outcomes = {row.observation_id: row for row in frame.snapshot.outcomes}
    features: dict[str, dict[str, object]] = {}
    transactions: dict[str, str] = {}
    with source._connection() as connection:
        has_inputs = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
        has_bindings = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='agent6_transaction_bindings_v1'"
        ).fetchone()
        if has_inputs and predictions:
            query = (
                "SELECT observation_id,inputs_json FROM transactions WHERE tenant_id=? "
                "AND project_id=? AND model_id=? AND model_version=? "
                "AND observed_at>=? AND observed_at<=? LIMIT 20001"
            )
            from agent6_mlops_lifecycle.monitoring.source import utc

            rows = connection.execute(
                query,
                (
                    source.scope.tenant_id,
                    source.scope.project_id,
                    source.scope.model_id,
                    frame.model_version,
                    utc(min(row.observed_at for row in predictions)),
                    utc(max(row.observed_at for row in predictions)),
                ),
            ).fetchall()
            if len(rows) > 20000:
                raise SourceUnavailableError("Fraud feature window exceeds the bounded input limit")
            indexed = {row.observation_id: row for row in predictions}
            for identity, raw in rows:
                prediction = indexed.get(identity)
                if prediction is None or prediction.input_ref is None:
                    continue
                if (
                    len(raw.encode()) > 16384
                    or "sha256:" + sha256(raw.encode()).hexdigest() != prediction.input_ref
                ):
                    raise SourceUnavailableError(
                        "Fraud input evidence failed its prediction-linked digest"
                    )
                data = json.loads(raw)
                if feature_json(data) != raw:
                    raise SourceUnavailableError("Fraud input evidence is not canonical")
                features[identity] = data
            if has_bindings:
                bindings = connection.execute(
                    "SELECT b.observation_id,b.transaction_id FROM "
                    "agent6_transaction_bindings_v1 b JOIN predictions p ON "
                    "p.observation_id=b.observation_id WHERE p.tenant_id=? AND p.project_id=? "
                    "AND p.model_id=? AND p.model_version=? AND p.observed_at>=? AND "
                    "p.observed_at<=? LIMIT 20001",
                    (
                        source.scope.tenant_id,
                        source.scope.project_id,
                        source.scope.model_id,
                        frame.model_version,
                        utc(min(row.observed_at for row in predictions)),
                        utc(max(row.observed_at for row in predictions)),
                    ),
                ).fetchall()
                if len(bindings) > 20000:
                    raise SourceUnavailableError("Fraud transaction window exceeds its bound")
                transactions = dict(bindings)
    pairs = []
    scores = []
    grouped: dict[tuple[str, str], list[tuple[int, int]]] = {}
    seen = set()
    duplicates = missing = eligible = simulated = 0
    for prediction in sorted(predictions, key=lambda row: (row.observed_at, row.observation_id)):
        identity = prediction.observation_id
        business = transactions.get(identity)
        key = ("transaction", business) if business else ("observation", identity)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        eligible += 1
        outcome = outcomes.get(identity)
        if outcome is None:
            continue
        if frame.kinds.get(identity) not in ("SYSTEM", "HUMAN"):
            simulated += 1
            continue
        actual, predicted = int(outcome.actual), int(prediction.predicted)
        pairs.append((actual, predicted))
        if prediction.positive_probability is not None:
            scores.append((actual, prediction.positive_probability))
        inputs = features.get(identity)
        if inputs is None:
            missing += 1
            continue
        bins = bucket_features(inputs)
        for slice_feature in policy.slice_features:
            grouped.setdefault((slice_feature, bins[slice_feature]), []).append((actual, predicted))
    metrics = confusion(pairs, policy)
    probabilities = probability_quality(scores, len(pairs), policy)
    coverage = len(pairs) / eligible if eligible else 0.0
    supported = (
        metrics.samples >= policy.min_labels
        and metrics.positive_labels >= policy.min_positive_labels
        and metrics.negative_labels >= policy.min_negative_labels
        and coverage >= policy.min_coverage
    )
    gates: dict[str, str] = {
        "recall": "INSUFFICIENT_EVIDENCE"
        if not supported
        else "PASS"
        if metrics.recall is not None and metrics.recall >= policy.min_recall
        else "FAIL",
        "false_positive_rate": "INSUFFICIENT_EVIDENCE"
        if not supported
        else "PASS"
        if metrics.false_positive_rate is not None
        and metrics.false_positive_rate <= policy.max_false_positive_rate
        else "FAIL",
        "business_cost": "NOT_CONFIGURED"
        if policy.max_cost_per_1000 is None
        else "INSUFFICIENT_EVIDENCE"
        if not supported
        else "PASS"
        if metrics.cost_per_1000 is not None and metrics.cost_per_1000 <= policy.max_cost_per_1000
        else "FAIL",
        "average_precision": "NOT_CONFIGURED"
        if policy.min_average_precision is None
        else "INSUFFICIENT_EVIDENCE"
        if probabilities.status != "ASSESSED"
        else "PASS"
        if probabilities.average_precision is not None
        and probabilities.average_precision >= policy.min_average_precision
        else "FAIL",
    }
    slices = []
    for (feature, bucket), values in sorted(grouped.items()):
        m = confusion(values, policy)
        enough = (
            m.samples >= policy.min_slice_labels
            and min(m.positive_labels, m.negative_labels) >= policy.min_slice_class_labels
        )
        good = (
            m.recall is not None
            and m.recall >= policy.min_recall
            and m.false_positive_rate is not None
            and m.false_positive_rate <= policy.max_false_positive_rate
        )
        slices.append(
            FraudSlice(
                feature=feature,
                bucket=bucket,
                status="INSUFFICIENT_EVIDENCE" if not enough else "PASS" if good else "FAIL",
                metrics=m,
            )
        )
    return FraudEvaluation.model_validate(
        {
            "policy": policy.model_dump(mode="json"),
            "status": "WAITING_FOR_LABELS"
            if not pairs
            else "INSUFFICIENT_EVIDENCE"
            if not supported
            else "FAIL"
            if "FAIL" in gates.values()
            else "INSUFFICIENT_EVIDENCE"
            if "INSUFFICIENT_EVIDENCE" in gates.values()
            else "PASS",
            "eligible_predictions": eligible,
            "outcome_coverage": coverage,
            "excluded_simulated_outcomes": simulated,
            "duplicate_transactions_excluded": duplicates,
            "missing_feature_evidence": missing,
            "metrics": metrics,
            "probabilities": probabilities,
            "slices": tuple(slices),
            "gates": gates,
            "cost_unit": policy.cost_unit if policy.false_negative_cost is not None else None,
            "limitations": (
                "Quality depends on representative, independently verified business "
                "outcomes; missing labels may bias every estimate.",
                "Probability metrics use only labeled observations with captured "
                "probabilities; legacy scores are not reconstructed.",
                "Wilson intervals describe sampling uncertainty, not model correctness "
                "probabilities; dependence and selection bias remain possible.",
                "Error cost uses explicitly configured constant costs, not measured "
                "transaction losses.",
                "Slice results are diagnostic; sparse slices remain insufficient and "
                "multiple comparisons need investigation.",
                "Threshold comparisons reuse the evaluation sample and are advisory only; "
                "validation is required before any change.",
            ),
        }
    )
