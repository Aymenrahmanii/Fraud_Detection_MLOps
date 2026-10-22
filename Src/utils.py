# src/utils.py

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    classification_report,
    confusion_matrix
)
import matplotlib.pyplot as plt
import seaborn as sns
import os


# ------------------------- METRICS -------------------------
def evaluate_model(y_true, y_pred, dataset_name="Validation"):
    """
    Print and return basic classification metrics and confusion matrix.
    """
    acc = accuracy_score(y_true, y_pred)
    prec = precision_score(y_true, y_pred)
    rec = recall_score(y_true, y_pred)
    f1 = f1_score(y_true, y_pred)
    roc_auc = roc_auc_score(y_true, y_pred)

    print(f"\n[{dataset_name} Metrics]")
    print(f"Accuracy:  {acc:.4f}")
    print(f"Precision: {prec:.4f}")
    print(f"Recall:    {rec:.4f}")
    print(f"F1 Score:  {f1:.4f}")
    print(f"ROC AUC:   {roc_auc:.4f}\n")

    print("Classification Report:\n", classification_report(y_true, y_pred))

    # Plot confusion matrix
    cm = confusion_matrix(y_true, y_pred)
    fig, ax = plt.subplots(figsize=(5, 4))
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', ax=ax)
    ax.set_title(f"{dataset_name} - Confusion Matrix")
    ax.set_xlabel('Predicted')
    ax.set_ylabel('Actual')

    os.makedirs("artifacts", exist_ok=True)
    cm_path = f"artifacts/confusion_matrix_{dataset_name.lower()}.png"
    plt.savefig(cm_path, bbox_inches='tight')
    plt.close()

    return {
        "accuracy": acc,
        "precision": prec,
        "recall": rec,
        "f1": f1,
        "roc_auc": roc_auc
    }


# ------------------------- DATA SPLIT -------------------------
def train_val_test_split(df, target_col, test_size=0.2, val_size=0.1, random_state=42):
    """
    Splits dataframe into train, validation, and test sets.
    """
    from sklearn.model_selection import train_test_split

    # First split: train + temp
    train_df, temp_df = train_test_split(
        df, test_size=(test_size + val_size), stratify=df[target_col], random_state=random_state
    )

    # Second split: validation + test
    val_relative = val_size / (test_size + val_size)
    val_df, test_df = train_test_split(
        temp_df, test_size=(1 - val_relative), stratify=temp_df[target_col], random_state=random_state
    )

    return train_df, val_df, test_df


# ------------------------- CLASS WEIGHTS -------------------------
def compute_class_weights(y):
    """
    Compute balanced class weights for imbalanced datasets.
    """
    from sklearn.utils.class_weight import compute_class_weight

    classes = np.unique(y)
    weights = compute_class_weight(class_weight='balanced', classes=classes, y=y)
    return dict(zip(classes, weights))
