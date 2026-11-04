import os
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import joblib
import pandas as pd
import streamlit as st

# Keep the training classes importable for the existing serialized model.
from Src.model import (  # noqa: F401
    FraudPipeline,
    FeatureEngineering,
    Preprocessing,
    LogTransformer,
)

from API.telemetry.operations import record_failure
from API.telemetry.config import TelemetryConfig
from API.telemetry.store import PredictionLogError, SQLitePredictionLog


def home_page():
    # Load model
    MODEL_PATH = os.path.join(
        os.path.dirname(__file__), "..", "Notebooks", "artifacts", "fraud_pipeline.pkl"
    )
    MODEL_PATH = os.path.abspath(MODEL_PATH)
    # print(MODEL_PATH)
    model_sha256 = sha256(Path(MODEL_PATH).read_bytes()).hexdigest()
    fp_loaded = joblib.load(MODEL_PATH)
    if sha256(Path(MODEL_PATH).read_bytes()).hexdigest() != model_sha256:
        st.error("The model artifact changed while loading. Please retry.")
        return
    # print(fp_loaded)

    # Page Title
    st.title("🔍 Fraud Detection App")

    # Problem Statement
    st.markdown("""
    ### Problem Statement
    Detect potentially fraudulent transactions using features like account age, payment method, and transaction time.
    - **0 → Legitimate Transaction**
    - **1 → Fraudulent Transaction**
    """)

    # Initialize session state for auto-fill
    if "inputs" not in st.session_state:
        st.session_state.inputs = {
            "Category": "shopping",
            "paymentMethod": "paypal",
            "isWeekend": 0,
            "numItems": 1,
            "localTime": 4.742303,
            "paymentMethodAgeDays": 0,
            "accountAgeDays": 1,
        }

    # Auto-fill buttons
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Load Legitimate Example"):
            st.session_state.inputs = {
                "Category": "shopping",
                "paymentMethod": "paypal",
                "isWeekend": 0,
                "numItems": 4,
                "localTime": 4.742303,
                "paymentMethodAgeDays": 0,
                "accountAgeDays": 4000,
            }
    with col2:
        if st.button("Load Fraudulent Example"):
            st.session_state.inputs = {
                "Category": "shopping",
                "paymentMethod": "paypal",
                "isWeekend": 0,
                "numItems": 4,
                "localTime": 4.742303,
                "paymentMethodAgeDays": 0,
                "accountAgeDays": 1,
            }

    # Input fields
    category = st.selectbox(
        "Category (e.g., electronics, shopping, food, other)",
        ["shopping", "food", "electronics", "other"],
        index=["shopping", "food", "electronics", "other"].index(
            st.session_state.inputs["Category"]
        ),
    )
    payment_method = st.selectbox(
        "Payment Method (PayPal, creditcard, debitcard)",
        ["paypal", "creditcard", "debitcard"],
        index=["paypal", "creditcard", "debitcard"].index(
            st.session_state.inputs["paymentMethod"]
        ),
    )
    is_weekend = st.selectbox(
        "Is Weekend? (1 = Yes, 0 = No)",
        [0, 1],
        index=[0, 1].index(st.session_state.inputs["isWeekend"]),
    )
    num_items = st.number_input(
        "Number of Items (count of items in transaction)",
        min_value=1,
        value=st.session_state.inputs["numItems"],
    )
    local_time = st.number_input(
        "Local Time (float: e.g., 4.742303)",
        value=float(st.session_state.inputs["localTime"]),
        format="%.6f",
    )
    payment_age = st.number_input(
        "Payment Method Age (days since method linked)",
        min_value=0,
        value=st.session_state.inputs["paymentMethodAgeDays"],
    )
    account_age = st.number_input(
        "Account Age (days since account created)",
        min_value=0,
        value=st.session_state.inputs["accountAgeDays"],
    )

    # Prediction logic
    business_transaction_id = st.text_input(
        "Business transaction ID (optional)",
        max_chars=160,
        help="Use the ID assigned by your business system so verified events can join automatically.",
    )
    if st.button("Predict Fraud"):
        input_data = pd.DataFrame(
            [
                {
                    "Category": category,
                    "paymentMethod": payment_method,
                    "isWeekend": is_weekend,
                    "numItems": num_items,
                    "localTime": local_time,
                    "paymentMethodAgeDays": payment_age,
                    "accountAgeDays": account_age,
                }
            ]
        )

        observed_at = datetime.now(UTC)
        started = time.perf_counter()
        prediction_log = None
        transaction_id = (
            business_transaction_id.strip()
            if isinstance(business_transaction_id, str)
            and business_transaction_id.strip()
            else str(uuid4())
        )
        try:
            telemetry = TelemetryConfig.from_env()
            prediction_log = SQLitePredictionLog(
                telemetry.database, telemetry.scope, model_sha256
            )
            started = time.perf_counter()
            fraud_prob = fp_loaded.predict_proba(input_data)[0]
            prediction = 1 if fraud_prob >= fp_loaded.best_threshold else 0
            event = prediction_log.append(
                prediction,
                observed_at,
                duration_ms=(time.perf_counter() - started) * 1000,
                inputs=input_data.iloc[0].to_dict(),
                channel="streamlit",
                positive_probability=float(fraud_prob),
                decision_threshold=float(fp_loaded.best_threshold),
                transaction_id=transaction_id,
            )
        except Exception:
            if prediction_log is not None:
                try:
                    record_failure(
                        prediction_log,
                        input_data.iloc[0].to_dict(),
                        (time.perf_counter() - started) * 1000,
                        channel="streamlit",
                    )
                except (PredictionLogError, OSError, ValueError):
                    pass
            st.error(
                "Prediction recording is unavailable or inference failed. Check the local service logs."
            )
            return
        st.caption("Monitoring observation: " + event.observation_id)
        st.caption("Business transaction ID: " + transaction_id)
        st.caption("Model version: " + event.model_version)

        if prediction == 1:
            st.error(
                f"🚨 Fraudulent Transaction!\nEstimated fraud probability: {fraud_prob * 100:.2f}%"
            )
        else:
            st.success(
                f"✅ Legitimate Transaction\nEstimated fraud probability: {fraud_prob * 100:.2f}%"
            )

    # Example outcomes
    st.markdown("""
    ---
    ### Example Outcomes
    **Legitimate Example:**
    ```
    {
      "Category": "shopping",
      "paymentMethod": "paypal",
      "isWeekend": 0,
      "numItems": 4,
      "localTime": 4.742303,
      "paymentMethodAgeDays": 0,
      "accountAgeDays": 4000
    }
    ```
    **Fraudulent Example:**
    ```
    {
      "Category": "shopping",
      "paymentMethod": "paypal",
      "isWeekend": 0,
      "numItems": 4,
      "localTime": 4.742303,
      "paymentMethodAgeDays": 0,
      "accountAgeDays": 1
    }
    ```
    """)
