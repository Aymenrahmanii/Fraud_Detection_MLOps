# src/main.py

import pandas as pd
import joblib
import os
import mlflow
from mlflow import MlflowClient

from model import FraudPipeline
from utils import evaluate_model
from config import (
    DATA_PATH,
    ARTIFACTS_PATH,
    MLFLOW_EXPERIMENT_NAME,
    TARGET_COLUMN,
)

from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_recall_curve, average_precision_score

# ------------------------- 1. Load Data -------------------------
def load_data():
    file_path = os.path.join(DATA_PATH, "payment_fraud.csv")
    df = pd.read_csv(file_path)
    print(f"Data loaded: {df.shape}")
    return df

# ------------------------- 2. Create Hold-out Sets -------------------------
def create_holdouts(df):
    df_clean = df.drop_duplicates(keep='first')

    # Holdout logic (similar to your notebook)
    n1, n2 = 99, 1
    n = 50

    holdout_class_0_A = df_clean[df_clean[TARGET_COLUMN] == 0].sample(n1, random_state=42)
    holdout_class_1_A = df_clean[df_clean[TARGET_COLUMN] == 1].sample(n2, random_state=42)
    holdout_df_A = pd.concat([holdout_class_0_A, holdout_class_1_A])
    train_df = df_clean.drop(holdout_df_A.index)

    holdout_class_0_B = train_df[train_df[TARGET_COLUMN] == 0].sample(n1, random_state=42)
    holdout_class_1_B = train_df[train_df[TARGET_COLUMN] == 1].sample(n2, random_state=42)
    holdout_df_B = pd.concat([holdout_class_0_B, holdout_class_1_B])
    train_df = train_df.drop(holdout_df_B.index)

    holdout_class_0_C = train_df[train_df[TARGET_COLUMN] == 0].sample(n, random_state=42)
    holdout_class_1_C = train_df[train_df[TARGET_COLUMN] == 1].sample(n, random_state=42)
    holdout_df_C = pd.concat([holdout_class_0_C, holdout_class_1_C])
    train_df = train_df.drop(holdout_df_C.index)

    print(f"Training size: {train_df.shape}")
    print(f"Holdout A: {holdout_df_A.shape} | Holdout B: {holdout_df_B.shape} | Holdout C: {holdout_df_C.shape}")

    return train_df, holdout_df_A, holdout_df_B, holdout_df_C

# ------------------------- 3. Train FraudPipeline -------------------------
def train_pipeline(train_df):
    fp = FraudPipeline(
        steps_to_apply=[
            "feature_engineering",
            "preprocessing",
            "model_training"
        ],
        resample_method="smote",
        model=LogisticRegression(),
    )
    pipeline, X_train, y_train, X_test, y_test = fp.train(train_df)
    print(f"Best Threshold Found: {fp.best_threshold:.3f}")
    return fp, pipeline, X_test, y_test

# ------------------------- 4. Evaluate on Hold-outs -------------------------
def evaluate_holdouts(fp, holdout_df, name):
    X = holdout_df.drop(columns=[TARGET_COLUMN])
    y = holdout_df[TARGET_COLUMN]
    preds = fp.predict_pipeline(X, use_optimal_threshold=True)
    metrics = evaluate_model(y, preds, dataset_name=name)
    return metrics

# ------------------------- 5. Save Combined Hold-out -------------------------
def save_combined_holdouts(holdout_A, holdout_B, holdout_C):
    combined_df = pd.concat([
        holdout_A.assign(dataset="A"),
        holdout_B.assign(dataset="B"),
        holdout_C.assign(dataset="C")
    ])
    save_path = os.path.join(DATA_PATH, "combined_holdout.csv")
    combined_df.to_csv(save_path, index=False)
    print(f"Combined holdout saved at {save_path}")
    return combined_df

# ------------------------- MAIN -------------------------
if __name__ == "__main__":
    # 1. Load data
    df = load_data()

    # 2. Create holdouts
    train_df, holdout_A, holdout_B, holdout_C = create_holdouts(df)

    # 3. Train pipeline
    fp, pipeline, X_test, y_test = train_pipeline(train_df)

    # 4. Evaluate on holdouts
    print("\nEvaluating Hold-out A:")
    evaluate_holdouts(fp, holdout_A, "Hold-out A")

    print("\nEvaluating Hold-out B:")
    evaluate_holdouts(fp, holdout_B, "Hold-out B")

    print("\nEvaluating Hold-out C:")
    evaluate_holdouts(fp, holdout_C, "Hold-out C")

    # 5. Save pipeline
    os.makedirs(ARTIFACTS_PATH, exist_ok=True)
    joblib.dump(fp, os.path.join(ARTIFACTS_PATH, "fraud_pipeline.pkl"))
    print(f"Pipeline saved to {ARTIFACTS_PATH}/fraud_pipeline.pkl")

    # 6. Save combined holdout
    combined_df = save_combined_holdouts(holdout_A, holdout_B, holdout_C)

    # 7. Register model (optional)
    mlflow.set_tracking_uri("file:./mlruns")
    client = MlflowClient()
    model_name = "FraudDetectionPipeline"
    # Example: register model alias (if already registered from notebook)
    # client.set_registered_model_alias(model_name, "champion", "7")
