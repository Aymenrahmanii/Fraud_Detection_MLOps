# src/model.py

import os
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler, MinMaxScaler
from sklearn.impute import SimpleImputer
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    confusion_matrix,
    precision_recall_curve,
    average_precision_score
)
from sklearn.ensemble import RandomForestClassifier
from imblearn.pipeline import Pipeline as ImbPipeline
from imblearn.over_sampling import SMOTE, ADASYN

import mlflow
import mlflow.pyfunc
from mlflow.models.signature import infer_signature
import joblib

# -------------------------
# Feature Engineering
# -------------------------
class FeatureEngineering(BaseEstimator, TransformerMixin):
    """
    Custom feature engineering transformer.
    Controlled via steps_to_apply list:
    - 'feature_engineering': enable feature engineering
    - 'interaction': Category x PaymentMethod
    - 'ratio': paymentMethodAgeDays / accountAgeDays
    - 'binning': bins for accountAgeDays
    - 'time_feature': bins for localTime
    """
    def __init__(self, steps_to_apply=None):
        self.steps_to_apply = steps_to_apply or []

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        X = X.copy()

        if 'feature_engineering' not in self.steps_to_apply:
            return X

        if 'interaction' in self.steps_to_apply:
            if 'Category' in X.columns and 'paymentMethod' in X.columns:
                X['Category_Payment'] = X['Category'] + '_' + X['paymentMethod']

        if 'ratio' in self.steps_to_apply:
            if 'paymentMethodAgeDays' in X.columns and 'accountAgeDays' in X.columns:
                X['payment_account_ratio'] = X['paymentMethodAgeDays'] / (X['accountAgeDays'] + 1)

        if 'binning' in self.steps_to_apply:
            if 'accountAgeDays' in X.columns:
                X['account_age_bin'] = pd.cut(
                    X['accountAgeDays'],
                    bins=[0, 90, 730, 2000],
                    labels=['new', 'medium', 'old']
                )

        if 'time_feature' in self.steps_to_apply:
            if 'localTime' in X.columns:
                bins = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
                labels = ['early_morning', 'morning', 'afternoon', 'evening', 'night']
                X['time_of_day'] = pd.cut(X['localTime'], bins=bins, labels=labels)

        return X


# -------------------------
# Log Transformer
# -------------------------
class LogTransformer(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        return np.log1p(np.array(X, dtype=float))

    def get_feature_names_out(self, input_features=None):
        return input_features


# -------------------------
# Preprocessing
# -------------------------
class Preprocessing(BaseEstimator, TransformerMixin):
    def __init__(self, categorical_features, skewed_features, symmetric_features,
                 steps_to_apply=None, random_state=42):
        self.categorical_features = categorical_features
        self.skewed_features = skewed_features
        self.symmetric_features = symmetric_features
        self.steps_to_apply = steps_to_apply or []
        self.random_state = random_state

        self.preprocessor = None

    def _build_pipeline(self):
        # Categorical pipeline
        cat_steps = []
        if 'impute' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            cat_steps.append(('imputer', SimpleImputer(strategy='most_frequent')))
        if 'encoding' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            cat_steps.append(('encoder', OneHotEncoder(handle_unknown='ignore', drop='first')))
        cat_pipeline = Pipeline(cat_steps) if cat_steps else 'passthrough'

        # Skewed numerical pipeline
        skewed_steps = []
        if 'impute' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            skewed_steps.append(('imputer', SimpleImputer(strategy='median')))
        if 'log_transform' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            skewed_steps.append(('log', LogTransformer()))
        if 'encoding' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            skewed_steps.append(('scaler', StandardScaler()))
        skewed_pipeline = Pipeline(skewed_steps) if skewed_steps else 'passthrough'

        # Symmetric numerical pipeline
        sym_steps = []
        if 'impute' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            sym_steps.append(('imputer', SimpleImputer(strategy='median')))
        if 'encoding' in self.steps_to_apply or 'preprocessing' in self.steps_to_apply:
            sym_steps.append(('scaler', MinMaxScaler()))
        sym_pipeline = Pipeline(sym_steps) if sym_steps else 'passthrough'

        # Combine pipelines
        self.preprocessor = ColumnTransformer(
            transformers=[
                ('cat', cat_pipeline, self.categorical_features),
                ('skew', skewed_pipeline, self.skewed_features),
                ('sym', sym_pipeline, self.symmetric_features)
            ],
            remainder='drop'
        )

    def fit(self, X, y=None):
        self._build_pipeline()
        self.preprocessor.fit(X)
        return self

    def transform(self, X):
        return self.preprocessor.transform(X)

    def fit_transform(self, X, y=None):
        self._build_pipeline()
        return self.preprocessor.fit_transform(X)


# -------------------------
# Fraud Pipeline Model
# -------------------------
class FraudPipeline(mlflow.pyfunc.PythonModel):
    FEATURE_ENG_SUBSTEPS = ['interaction', 'ratio', 'binning', 'time_feature']
    PREPROCESS_SUBSTEPS = ['encoding', 'impute', 'log_transform', 'smote']

    def __init__(self, steps_to_apply=None, model=None, test_size=0.2,
                 random_state=42, resample_method="smote", experiment_name="FraudDetection"):
        self.steps_to_apply = self.expand_steps(steps_to_apply)
        self.model = model or RandomForestClassifier(class_weight='balanced', random_state=random_state)
        self.test_size = test_size
        self.random_state = random_state
        self.resample_method = resample_method
        self.experiment_name = experiment_name

        self.pipeline = None
        self.best_threshold = 0.5

        self.categorical = ['Category', 'paymentMethod', 'isWeekend']
        self.skewed = ['numItems', 'localTime', 'paymentMethodAgeDays']
        self.symmetric = ['accountAgeDays']
        self.target = 'label'

        mlflow.set_experiment(self.experiment_name)

    # Expand steps to ensure substeps are valid
    def expand_steps(self, steps_to_apply):
        steps = set(steps_to_apply or [])

        # Feature Engineering validation
        fe_substeps = steps.intersection(self.FEATURE_ENG_SUBSTEPS)
        if fe_substeps and 'feature_engineering' not in steps:
            raise ValueError(f"Feature engineering sub-steps {fe_substeps} provided without 'feature_engineering'")
        if 'feature_engineering' in steps and not fe_substeps:
            steps.update(self.FEATURE_ENG_SUBSTEPS)

        # Preprocessing validation
        pre_substeps = steps.intersection(self.PREPROCESS_SUBSTEPS)
        if pre_substeps and 'preprocessing' not in steps:
            raise ValueError(f"Preprocessing sub-steps {pre_substeps} provided without 'preprocessing'")
        if 'preprocessing' in steps and not pre_substeps:
            steps.update(self.PREPROCESS_SUBSTEPS)

        return list(steps)

    # Train model
    def train(self, df):
        X = df.drop(columns=[self.target])
        y = df[self.target]

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, stratify=y, test_size=self.test_size, random_state=self.random_state, shuffle=True
        )

        feature_engineer = FeatureEngineering(steps_to_apply=self.steps_to_apply) \
            if any(s in self.steps_to_apply for s in self.FEATURE_ENG_SUBSTEPS) else 'passthrough'

        preprocessor = Preprocessing(
            self.categorical, self.skewed, self.symmetric,
            steps_to_apply=self.steps_to_apply,
            random_state=self.random_state
        ) if any(s in self.steps_to_apply for s in self.PREPROCESS_SUBSTEPS) else 'passthrough'

        self.pipeline = ImbPipeline([
            ('feature_engineering', feature_engineer),
            ('preprocessing', preprocessor),
            ('model', self.model)
        ])

        # Transform
        X_train_transformed = self.pipeline[:-1].fit_transform(X_train, y_train)
        X_test_transformed = self.pipeline[:-1].transform(X_test)

        # Apply SMOTE/ADASYN
        if 'smote' in self.steps_to_apply:
            if self.resample_method == "smote":
                sampler = SMOTE(random_state=self.random_state, sampling_strategy='minority', k_neighbors=5)
            elif self.resample_method == "adasyn":
                sampler = ADASYN(random_state=self.random_state, sampling_strategy='minority', n_neighbors=5)
            else:
                raise ValueError("resample_method must be 'smote' or 'adasyn'")
            X_train_transformed, y_train = sampler.fit_resample(X_train_transformed, y_train)

        # Train and log
        with mlflow.start_run(run_name=f"{type(self.model).__name__}_run"):
            mlflow.log_param("steps_to_apply", self.steps_to_apply)
            mlflow.log_param("resample_method", self.resample_method)
            mlflow.log_param("test_size", self.test_size)
            mlflow.log_param("model", type(self.model).__name__)
            mlflow.log_param("categorical_features", self.categorical)
            mlflow.log_param("skewed_features", self.skewed)
            mlflow.log_param("symmetric_features", self.symmetric)

            self.model.fit(X_train_transformed, y_train)

            # Threshold tuning
            y_train_proba = self.model.predict_proba(X_train_transformed)[:, 1]
            precision, recall, thresholds = precision_recall_curve(y_train, y_train_proba)
            f1_scores = 2 * (precision * recall) / (precision + recall + 1e-6)
            best_idx = f1_scores.argmax()
            self.best_threshold = thresholds[best_idx] if best_idx < len(thresholds) else 0.5
            mlflow.log_param("optimal_threshold", self.best_threshold)

            # Evaluate and log metrics
            y_train_pred = self.model.predict(X_train_transformed)
            y_test_pred = self.model.predict(X_test_transformed)
            self._log_metrics(self._calculate_metrics(y_train, y_train_pred, prefix="train"))
            self._log_metrics(self._calculate_metrics(y_test, y_test_pred, prefix="test"))

            # Log PR-AUC
            y_test_proba = self.model.predict_proba(X_test_transformed)[:, 1]
            pr_auc = average_precision_score(y_test, y_test_proba)
            mlflow.log_metric("test_pr_auc", pr_auc)

            # Save artifacts
            self._log_pr_curve(y_test, y_test_proba, pr_auc)
            self._log_confusion_matrix(y_test, y_test_pred)

            # Log model
            signature = infer_signature(X_train, self.model.predict(X_train_transformed))
            mlflow.pyfunc.log_model(name="fraud_pipeline", python_model=self, signature=signature)

            # Save pipeline locally
            os.makedirs("artifacts", exist_ok=True)
            joblib.dump(self, "artifacts/fraud_pipeline.pkl")
            mlflow.log_artifacts("artifacts")

        return self.pipeline, X_train_transformed, y_train, X_test_transformed, y_test

    # MLflow predict
    def predict(self, context, model_input):
        return self.predict_pipeline(model_input, use_optimal_threshold=True)

    # Predict pipeline
    def predict_pipeline(self, df, use_optimal_threshold=False):
        if not self.pipeline:
            raise ValueError("Pipeline not trained.")
        transformed = self.pipeline[:-1].transform(df) if isinstance(df, pd.DataFrame) else df
        if use_optimal_threshold:
            probs = self.pipeline[-1].predict_proba(transformed)[:, 1]
            return (probs >= self.best_threshold).astype(int)
        else:
            return self.pipeline[-1].predict(transformed)

    # Predict probabilities
    def predict_proba(self, df):
        if not self.pipeline:
            raise ValueError("Pipeline not trained.")
        transformed = self.pipeline[:-1].transform(df) if isinstance(df, pd.DataFrame) else df
        return self.pipeline[-1].predict_proba(transformed)[:, 1]

    # Helper: Calculate metrics
    def _calculate_metrics(self, y_true, y_pred, prefix=""):
        return {
            f"{prefix}_accuracy": accuracy_score(y_true, y_pred),
            f"{prefix}_precision": precision_score(y_true, y_pred),
            f"{prefix}_recall": recall_score(y_true, y_pred),
            f"{prefix}_f1": f1_score(y_true, y_pred)
        }

    # Helper: Log metrics
    def _log_metrics(self, metrics):
        for k, v in metrics.items():
            mlflow.log_metric(k, v)

    # Helper: Log PR curve
    def _log_pr_curve(self, y_true, y_proba, pr_auc):
        precision, recall, _ = precision_recall_curve(y_true, y_proba)
        plt.figure(figsize=(6, 4))
        plt.plot(recall, precision, label=f'PR curve (AUC={pr_auc:.3f})')
        plt.xlabel('Recall')
        plt.ylabel('Precision')
        plt.title('Precision-Recall Curve (Test)')
        plt.legend()
        plt.grid(True)
        path = "artifacts/pr_curve.png"
        plt.savefig(path, bbox_inches='tight')
        plt.close()
        mlflow.log_artifact(path, "precision_recall_curve")

    # Helper: Log confusion matrix
    def _log_confusion_matrix(self, y_true, y_pred):
        cm = confusion_matrix(y_true, y_pred)
        fig, ax = plt.subplots(figsize=(6, 4))
        sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', ax=ax)
        ax.set_title('Confusion Matrix')
        ax.set_xlabel('Predicted')
        ax.set_ylabel('Actual')
        path = "artifacts/confusion_matrix.png"
        plt.savefig(path, bbox_inches='tight')
        plt.close(fig)
        mlflow.log_artifact(path, "confusion_matrix")
