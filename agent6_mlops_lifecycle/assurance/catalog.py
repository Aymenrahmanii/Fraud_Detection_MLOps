"""One-time import of existing project labels; scheduled jobs read SQLite only."""

import csv
import math
from hashlib import sha256
from pathlib import Path

from pydantic import JsonValue

from agent6_mlops_lifecycle.assurance.models import BenchmarkDataset, BenchmarkRow
from agent6_mlops_lifecycle.assurance.store import AssuranceStore

FEATURES = (
    "accountAgeDays",
    "numItems",
    "localTime",
    "paymentMethod",
    "paymentMethodAgeDays",
    "Category",
    "isWeekend",
)


def import_fraud_holdout(path: Path, store: AssuranceStore) -> str:
    if path.stat().st_size > 8_000_000:
        raise ValueError("Benchmark import exceeds its size limit")
    raw = path.read_bytes()
    rows: list[BenchmarkRow] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if set(reader.fieldnames or ()) != {*FEATURES, "label", "dataset"}:
            raise ValueError("Unexpected fraud holdout schema")
        for row in reader:
            if (
                len(rows) >= 20000
                or row["label"] not in ("0", "1")
                or row["dataset"] not in ("A", "B", "C")
            ):
                raise ValueError("Invalid holdout row or sample limit exceeded")
            inputs: dict[str, JsonValue] = {}
            for name in FEATURES:
                text = row[name]
                if not text:
                    inputs[name] = None
                elif name in ("paymentMethod", "Category"):
                    inputs[name] = text
                elif name in ("accountAgeDays", "numItems"):
                    inputs[name] = int(text)
                else:
                    number = float(text)
                    if not math.isfinite(number):
                        raise ValueError("Nonfinite benchmark feature")
                    inputs[name] = number
            identity = repr(sorted(inputs.items()))
            if identity in seen:
                raise ValueError("Duplicate benchmark features would bias evaluation")
            seen.add(identity)
            rows.append(
                BenchmarkRow(
                    inputs=inputs, actual=1 if row["label"] == "1" else 0, group=row["dataset"]
                )
            )
    if path.read_bytes() != raw:
        raise ValueError("Benchmark source changed while importing")
    dataset = BenchmarkDataset(
        source_sha256=sha256(raw).hexdigest(), source_name=path.name, rows=tuple(rows)
    )
    return store.put("dataset", dataset.model_dump(mode="json"))
