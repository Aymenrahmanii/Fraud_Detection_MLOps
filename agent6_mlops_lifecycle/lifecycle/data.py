"""Conservative feature-group identities across CSV and serving float representations."""

import json
from hashlib import sha256
from math import isfinite, isnan
from typing import Any

FEATURES = (
    "accountAgeDays",
    "numItems",
    "localTime",
    "paymentMethod",
    "paymentMethodAgeDays",
    "Category",
    "isWeekend",
)


def eligible_training_inputs(inputs: dict[str, Any]) -> bool:
    """Respect the serving model's nullable fields without accepting malformed rows."""
    from API.telemetry.operations import bucket_features

    if set(inputs) != set(FEATURES):
        return False
    return all(
        value != "INVALID" and (value != "MISSING" or name in {"Category", "isWeekend"})
        for name, value in bucket_features(inputs).items()
    )


def feature_fingerprint(record: dict[str, Any]) -> str:
    values: list[str | float | None] = []
    for key in FEATURES:
        value = record[key]
        if value is None or (isinstance(value, float) and isnan(value)):
            values.append(None)
        elif key in ("paymentMethod", "Category"):
            values.append(str(value))
        else:
            number = float(value)
            if not isfinite(number):
                raise ValueError("Nonfinite numeric features cannot enter a candidate split")
            # Near-identical feature groups are excluded conservatively, never separated
            # by sub-nanosecond CSV floating-point roundoff.
            values.append(round(number, 8))
    return sha256(json.dumps(values, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def partition_verified_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reserve recent complete feature groups; older groups may augment training."""
    ordered = sorted(rows, key=lambda row: (row["observed_at"], row["observation_id"]))
    count = min(len(ordered), max(50, (len(ordered) * 2 + 4) // 5))
    held_out = {feature_fingerprint(row["inputs"]) for row in ordered[-count:]}
    fit = [row for row in ordered if feature_fingerprint(row["inputs"]) not in held_out]
    guard = [row for row in ordered if feature_fingerprint(row["inputs"]) in held_out]
    return fit, guard
