# src/config.py

import os

# ------------------------- PATHS -------------------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DATA_PATH = os.path.join(BASE_DIR, "data")
ARTIFACTS_PATH = os.path.join(BASE_DIR, "Src/artifacts")
MLRUNS_PATH = os.path.join(BASE_DIR, "Src/mlruns")

# Ensure directories exist
os.makedirs(DATA_PATH, exist_ok=True)
os.makedirs(ARTIFACTS_PATH, exist_ok=True)

# ------------------------- MLflow -------------------------
MLFLOW_EXPERIMENT_NAME = "FraudDetection"
MLFLOW_TRACKING_URI = f"file://{MLRUNS_PATH}"   # local file-based tracking

# ------------------------- MODEL CONFIG -------------------------
TARGET_COLUMN = "label"

# Feature groups
CATEGORICAL_FEATURES = ["Category", "paymentMethod", "isWeekend"]
SKEWED_FEATURES = ["numItems", "localTime", "paymentMethodAgeDays"]
SYMMETRIC_FEATURES = ["accountAgeDays"]

# ------------------------- TRAINING -------------------------
TEST_SIZE = 0.2
VAL_SIZE = 0.1
RANDOM_STATE = 42
RESAMPLE_METHOD = "smote"  # options: "smote" or "adasyn"

# Default steps to apply
DEFAULT_STEPS = [
    "feature_engineering",
    "interaction",
    "ratio",
    "binning",
    "time_feature",
    "preprocessing",
    "encoding",
    "impute",
    "log_transform",
    "smote",
    "model_training"
]