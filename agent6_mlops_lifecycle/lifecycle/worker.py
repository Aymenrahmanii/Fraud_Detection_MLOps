"Actual fraud candidate training, validation, untouched guard evaluation and MLflow registration."

import contextlib
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

FEATURES = [
    "accountAgeDays",
    "numItems",
    "localTime",
    "paymentMethod",
    "paymentMethodAgeDays",
    "Category",
    "isWeekend",
]


def run(request: dict[str, Any]) -> dict[str, Any]:
    import mlflow
    import numpy as np
    import pandas as pd
    from mlflow.models import infer_signature
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import (
        balanced_accuracy_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
    )
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    from agent6_mlops_lifecycle.assurance.worker import artifact_digest
    from agent6_mlops_lifecycle.lifecycle.model import ThresholdModel

    project = Path(request["project_root"]).resolve(strict=True)
    output = Path(request["output"]).resolve()
    if not output.is_relative_to(project / "artifacts/agent6"):
        raise ValueError("Candidate output must stay in owned artifact storage")
    output.mkdir(parents=True, exist_ok=False)
    source = Path(request["training_data"]).resolve(strict=True)
    raw = source.read_bytes()
    if len(raw) > 64_000_000:
        raise ValueError("Training dataset exceeds the bounded local input limit")
    frame = pd.read_csv(source)
    if set(frame.columns) != {*FEATURES, "label"} or not frame.label.isin([0, 1]).all():
        raise ValueError("Training schema/labels differ from the declared fraud task")
    if source.read_bytes() != raw:
        raise ValueError("Training source changed during snapshot")
    guard = pd.DataFrame(request["benchmark_rows"])
    live = request.get("live_guard", [])
    production_fit = request.get("production_fit", [])
    if production_fit:
        additions = pd.DataFrame(
            [{**row["inputs"], "label": row["actual"]} for row in production_fit]
        )
        frame = pd.concat([frame, additions], ignore_index=True)

    from agent6_mlops_lifecycle.lifecycle.data import feature_fingerprint as fingerprint

    excluded = {fingerprint(row) for row in guard.to_dict("records")}
    excluded.update(fingerprint(row["inputs"]) for row in live)
    rows = frame.to_dict("records")
    hashes = [fingerprint(row) for row in rows]
    labels_by_key: dict[str, int] = {}
    for key, row in zip(hashes, rows, strict=True):
        if key in labels_by_key and labels_by_key[key] != int(row["label"]):
            raise ValueError("Duplicate features have conflicting labels")
        labels_by_key[key] = int(row["label"])
    keep = []
    seen = set()
    for index, key in enumerate(hashes):
        if key not in excluded and key not in seen:
            keep.append(index)
            seen.add(key)
    pool = frame.iloc[keep].copy()
    if len(pool) < 500 or pool.label.value_counts().min() < 30:
        raise ValueError("Too few independent training rows or class examples")
    fit, validation = train_test_split(pool, test_size=0.2, stratify=pool.label, random_state=42)
    numeric = [key for key in FEATURES if key not in ("paymentMethod", "Category")]
    preprocess = ColumnTransformer(
        [
            ("numeric", SimpleImputer(strategy="median"), numeric),
            (
                "categorical",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        ("encode", OneHotEncoder(handle_unknown="ignore")),
                    ]
                ),
                ["paymentMethod", "Category"],
            ),
        ]
    )
    pipeline = Pipeline(
        [
            ("preprocess", preprocess),
            (
                "model",
                RandomForestClassifier(
                    n_estimators=120,
                    max_depth=14,
                    min_samples_leaf=2,
                    class_weight="balanced_subsample",
                    random_state=42,
                    n_jobs=1,
                ),
            ),
        ]
    )
    fit_hashes = sorted(fingerprint(row) for row in fit.to_dict("records"))
    validation_hashes = sorted(fingerprint(row) for row in validation.to_dict("records"))
    if set(fit_hashes) & (set(validation_hashes) | excluded) or set(validation_hashes) & excluded:
        raise ValueError("Training/validation/guard feature groups overlap")
    (output / "split-manifest.json").write_text(
        json.dumps(
            {
                "source_sha256": sha256(raw).hexdigest(),
                "feature_identity": "numeric-round-8-v1",
                "fit_feature_sha256": fit_hashes,
                "validation_feature_sha256": validation_hashes,
                "excluded_guard_feature_sha256": sorted(excluded),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    pipeline.fit(fit[FEATURES], fit.label)
    probabilities = pipeline.predict_proba(validation[FEATURES])[:, 1]
    thresholds = np.linspace(0.05, 0.95, 19)
    threshold = float(
        max(
            thresholds,
            key=lambda value: f1_score(validation.label, probabilities >= value, zero_division=0),
        )
    )
    model = ThresholdModel(pipeline, threshold)

    def metrics(actual: Any, predicted: Any) -> dict[str, Any]:
        tn, fp, fn, tp = confusion_matrix(actual, predicted, labels=[0, 1]).ravel()
        return {
            "f1": float(f1_score(actual, predicted, zero_division=0)),
            "precision": float(precision_score(actual, predicted, zero_division=0)),
            "recall": float(recall_score(actual, predicted, zero_division=0)),
            "balanced_accuracy": float(balanced_accuracy_score(actual, predicted)),
            "false_positive_rate": float(fp / (fp + tn)) if fp + tn else None,
            "samples": len(actual),
            "positive_labels": int(sum(actual)),
            "tp": int(tp),
            "fp": int(fp),
            "fn": int(fn),
            "tn": int(tn),
        }

    historical = metrics(guard.label.to_numpy(), model.predict(None, guard[FEATURES]))
    candidate_live = champion_live = None
    if live:
        inputs = pd.DataFrame([row["inputs"] for row in live])
        actual = [row["actual"] for row in live]
        candidate_live = metrics(actual, model.predict(None, inputs[FEATURES]))
        champion_live = metrics(actual, [row["predicted"] for row in live])
    improvement_ci = None
    if live:
        actual_array = np.asarray([row["actual"] for row in live])
        champion_array = np.asarray([row["predicted"] for row in live])
        candidate_array = model.predict(
            None, pd.DataFrame([row["inputs"] for row in live])[FEATURES]
        )
        generator = np.random.default_rng(42)
        differences = []
        for _ in range(300):
            indices = generator.integers(0, len(live), size=len(live))
            differences.append(
                f1_score(actual_array[indices], candidate_array[indices], zero_division=0)
                - f1_score(actual_array[indices], champion_array[indices], zero_division=0)
            )
        improvement_ci = [float(value) for value in np.quantile(differences, [0.025, 0.975])]
    policy = request["policy"]
    selected = candidate_live or historical
    gates = {
        "minimum_f1": selected["f1"] >= policy["min_f1"],
        "minimum_recall": selected["recall"] >= policy["min_recall"],
        "false_positive_rate": selected["false_positive_rate"] is not None
        and selected["false_positive_rate"] <= policy["max_false_positive_rate"],
        "historical_regression_guard": historical["f1"] >= policy["min_f1"]
        and historical["recall"] >= policy["min_recall"],
        "champion_improvement": champion_live is None
        or selected["f1"] >= champion_live["f1"] + policy["min_f1_improvement"],
        "champion_recall_guard": champion_live is None
        or selected["recall"] >= champion_live["recall"] - policy["max_recall_drop"],
    }
    qualified = all(gates.values())
    mlflow.set_tracking_uri("sqlite:///" + str(project / "artifacts/agent6/mlflow.db"))
    mlflow.set_experiment("Agent6-Fraud-Candidates")
    with mlflow.start_run() as tracked:
        mlflow.log_params(
            {
                "recipe": "fraud-random-forest-v1",
                "training_source_sha256": sha256(raw).hexdigest(),
                "benchmark_dataset_id": request["benchmark_dataset_id"],
                "threshold": threshold,
                "fit_samples": len(fit),
                "validation_samples": len(validation),
                "production_training_rows": len(production_fit),
                "production_guard_rows": len(live),
                "excluded_guard_feature_hashes": len(excluded),
            }
        )
        mlflow.log_artifact(str(output / "split-manifest.json"))
        mlflow.log_metrics(
            {
                "candidate_guard_f1": selected["f1"],
                "candidate_guard_recall": selected["recall"],
                "historical_f1": historical["f1"],
            }
        )
        artifact = output / "model"
        mlflow.pyfunc.save_model(
            path=str(artifact),
            python_model=model,
            signature=infer_signature(fit[FEATURES], model.predict(None, fit[FEATURES])),
            input_example=fit[FEATURES].head(2),
            pip_requirements=[
                "mlflow>=3.5,<4",
                "scikit-learn==1.6.1",
                "pandas>=2,<3",
                "cloudpickle>=3,<4",
            ],
        )
        version = None
        if qualified:
            registered = mlflow.register_model(
                artifact.as_uri(),
                "fraud-agent6-candidates",
                tags={
                    "source_report_id": request["source_report_id"],
                    "review_status": "PENDING",
                    "recipe": "fraud-random-forest-v1",
                },
            )
            version = str(registered.version)
        result = {
            "qualified": qualified,
            "improvement_ci95": improvement_ci,
            "gates": gates,
            "historical": historical,
            "candidate_live": candidate_live,
            "champion_live": champion_live,
            "verified_live_guard": bool(live),
            "guard_origin": request["guard_origin"],
            "training_source_sha256": sha256(raw).hexdigest(),
            "benchmark_dataset_id": request["benchmark_dataset_id"],
            "training_independence": "GUARD_FEATURES_EXCLUDED",
            "fit_feature_hashes_sha256": sha256(
                "".join(sorted(fingerprint(row) for row in fit.to_dict("records"))).encode()
            ).hexdigest(),
            "fit_samples": len(fit),
            "validation_samples": len(validation),
            "production_training_rows": len(production_fit),
            "production_guard_rows": len(live),
            "threshold": threshold,
            "candidate_path": str(artifact),
            "candidate_sha256": artifact_digest(artifact),
            "mlflow_run_id": tracked.info.run_id,
            "registered_model": "fraud-agent6-candidates",
            "registered_version": version,
        }
        (output / "evaluation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read(2_000_001))
        with contextlib.redirect_stdout(sys.stderr):
            result = run(request)
        print(json.dumps(result, allow_nan=False))
    except Exception:
        import traceback

        traceback.print_exc(file=sys.stderr)
        print(
            (
                "Candidate training failed; inspect bounded schema, class support and "
                "compatible runtime"
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
