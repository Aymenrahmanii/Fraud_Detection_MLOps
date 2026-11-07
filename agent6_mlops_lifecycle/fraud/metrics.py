"""Auditable binary confusion, PR ranking, calibration and cost calculations."""

from math import sqrt

from agent6_mlops_lifecycle.fraud.models import (
    CalibrationBin,
    Confusion,
    FraudSettings,
    ProbabilityQuality,
    ThresholdPoint,
)


def wilson(successes: int, total: int) -> tuple[float, float] | None:
    if not total:
        return None
    z = 1.959963984540054
    rate, z2 = successes / total, z * z
    center = (rate + z2 / (2 * total)) / (1 + z2 / total)
    spread = z * sqrt(rate * (1 - rate) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return max(0.0, center - spread), min(1.0, center + spread)


def confusion(pairs: list[tuple[int, int]], policy: FraudSettings) -> Confusion:
    tp = sum(actual == predicted == 1 for actual, predicted in pairs)
    tn = sum(actual == predicted == 0 for actual, predicted in pairs)
    fp = sum(actual == 0 and predicted == 1 for actual, predicted in pairs)
    fn = sum(actual == 1 and predicted == 0 for actual, predicted in pairs)
    cost = None
    if pairs and policy.false_negative_cost is not None and policy.false_positive_cost is not None:
        cost = fn * policy.false_negative_cost + fp * policy.false_positive_cost
    return Confusion(
        samples=len(pairs),
        positive_labels=tp + fn,
        negative_labels=tn + fp,
        true_positives=tp,
        true_negatives=tn,
        false_positives=fp,
        false_negatives=fn,
        precision=tp / (tp + fp) if tp + fp else None,
        recall=tp / (tp + fn) if tp + fn else None,
        f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        false_positive_rate=fp / (fp + tn) if fp + tn else None,
        missed_fraud_rate=fn / (fn + tp) if fn + tp else None,
        precision_interval95=wilson(tp, tp + fp),
        recall_interval95=wilson(tp, tp + fn),
        false_positive_interval95=wilson(fp, fp + tn),
        estimated_error_cost=cost,
        cost_per_1000=cost * 1000 / len(pairs) if cost is not None and pairs else None,
    )


def point(
    threshold: float, pairs: list[tuple[int, float]], policy: FraudSettings
) -> ThresholdPoint:
    m = confusion([(y, int(p >= threshold)) for y, p in pairs], policy)
    return ThresholdPoint(
        threshold=threshold,
        precision=m.precision,
        recall=m.recall,
        false_positive_rate=m.false_positive_rate,
        cost_per_1000=m.cost_per_1000,
    )


def probability_quality(
    pairs: list[tuple[int, float]], labeled: int, policy: FraudSettings
) -> ProbabilityQuality:
    n = len(pairs)
    positives = sum(y for y, _ in pairs)
    negatives = n - positives
    coverage = n / labeled if labeled else 0.0
    supported = (
        n >= policy.min_probability_labels
        and positives >= policy.min_positive_labels
        and negatives >= policy.min_negative_labels
        and coverage >= policy.min_probability_coverage
    )
    bins = []
    for i in range(10):
        values = [(y, p) for y, p in pairs if min(9, int(p * 10)) == i]
        if values:
            bins.append(
                CalibrationBin(
                    lower=i / 10,
                    upper=(i + 1) / 10,
                    samples=len(values),
                    mean_probability=sum(p for _, p in values) / len(values),
                    observed_fraud_rate=sum(y for y, _ in values) / len(values),
                )
            )
    # Group ties before calculating recall increments; never depend on input ordering.
    groups: dict[float, list[int]] = {}
    for y, p in pairs:
        groups.setdefault(p, []).append(y)
    tp = fp = 0
    ap = 0.0
    auc_pairs = 0.0
    curve = []
    for p, labels in sorted(groups.items(), reverse=True):
        a, b = sum(labels), len(labels) - sum(labels)
        tp += a
        fp += b
        if positives:
            ap += a / positives * tp / (tp + fp)
        # Each positive outranks all remaining lower-score negatives; ties count half.
        auc_pairs += a * (negatives - fp + 0.5 * b)
        cost = (
            ((positives - tp) * policy.false_negative_cost + fp * policy.false_positive_cost)
            if policy.false_negative_cost is not None and policy.false_positive_cost is not None
            else None
        )
        curve.append(
            ThresholdPoint(
                threshold=p,
                precision=tp / (tp + fp),
                recall=tp / positives if positives else None,
                false_positive_rate=fp / negatives if negatives else None,
                cost_per_1000=cost * 1000 / n if cost is not None and n else None,
            )
        )
    if len(curve) > 51:
        curve = [curve[round(i * (len(curve) - 1) / 50)] for i in range(51)]
    grid = tuple(point(i / 20, pairs, policy) for i in range(21)) if pairs else ()
    eligible = [
        row
        for row in grid
        if row.cost_per_1000 is not None
        and row.recall is not None
        and row.recall >= policy.min_recall
        and row.false_positive_rate is not None
        and row.false_positive_rate <= policy.max_false_positive_rate
    ]
    advised = (
        min(eligible, key=lambda row: (row.cost_per_1000 or 0, -row.threshold)).threshold
        if supported and eligible
        else None
    )
    return ProbabilityQuality(
        status="ASSESSED" if supported else "INSUFFICIENT_EVIDENCE" if n else "UNAVAILABLE",
        samples=n,
        positive_labels=positives,
        negative_labels=negatives,
        labeled_probability_coverage=coverage,
        average_precision=ap if positives and negatives else None,
        roc_auc=auc_pairs / (positives * negatives) if positives and negatives else None,
        brier_score=sum((p - y) ** 2 for y, p in pairs) / n if n else None,
        expected_calibration_error=sum(
            row.samples * abs(row.mean_probability - row.observed_fraud_rate) for row in bins
        )
        / n
        if n
        else None,
        calibration_bins=tuple(bins),
        precision_recall_curve=tuple(curve),
        threshold_comparison=grid,
        advisory_cost_threshold=advised,
    )
