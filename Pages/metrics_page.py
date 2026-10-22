import streamlit as st

def metrics_page():
    st.title("📊 Model Metrics")

    st.markdown("""
    ### Overview
    Metrics are averaged across the three hold-outs.
    
    ---
    ## Data Distribution

    - **Original Data Size:** (39221, 8)  
    - **After Removing Duplicates:** (36188, 8)  
    - **Training Data:** (35788, 8)  
    - **Hold-out A Data:** (100, 8)  
    - **Hold-out B Data:** (100, 8)  
    - **Hold-out C Data:** (100, 8)  

    This split ensures **balanced evaluation** while preserving most data for training.

    ---
    """)

    # Individual metrics from each hold-out
    metrics = {
        "A": {"accuracy": 0.97, "precision": 0.25, "recall": 1.00, "f1": 0.40},
        "B": {"accuracy": 0.99, "precision": 0.50, "recall": 1.00, "f1": 0.67},
        "C": {"accuracy": 0.98, "precision": 0.98, "recall": 0.98, "f1": 0.98},
    }

    # Calculate average metrics
    avg_metrics = {
        "accuracy": round((metrics["A"]["accuracy"] + metrics["B"]["accuracy"] + metrics["C"]["accuracy"]) / 3, 4),
        "precision": round((metrics["A"]["precision"] + metrics["B"]["precision"] + metrics["C"]["precision"]) / 3, 4),
        "recall": round((metrics["A"]["recall"] + metrics["B"]["recall"] + metrics["C"]["recall"]) / 3, 4),
        "f1": round((metrics["A"]["f1"] + metrics["B"]["f1"] + metrics["C"]["f1"]) / 3, 4),
    }

    # Display average metrics
    st.subheader("**Average Metrics (A + B + C)**")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Accuracy", f"{avg_metrics['accuracy']*100:.2f}%")
    col2.metric("Precision", f"{avg_metrics['precision']*100:.2f}%")
    col3.metric("Recall", f"{avg_metrics['recall']*100:.2f}%")
    col4.metric("F1 Score", f"{avg_metrics['f1']*100:.2f}%")

    st.markdown("---")

    # Detailed metrics per holdout
    st.subheader("**Detailed Metrics per Hold-out**")

    st.markdown("""
    **Hold-out A**
    - Accuracy: 97.00%
    - Precision: 25.00%
    - Recall: 100.00%
    - F1 Score: 40.00%

    **Hold-out B**
    - Accuracy: 99.00%
    - Precision: 50.00%
    - Recall: 100.00%
    - F1 Score: 66.67%

    **Hold-out C**
    - Accuracy: 98.00%
    - Precision: 98.00%
    - Recall: 98.00%
    - F1 Score: 98.00%
    """)

    st.markdown("---")

    # Confusion matrix
    st.subheader("**Confusion Matrix**")
    st.image("Src/artifacts/confusion_matrix.png", caption="Confusion Matrix")

    st.markdown("""
    ---
    **Interpretation:**
    - High recall (100% in A & B) ensures **fraud cases are not missed**.
    - Slight drop in precision for A & B indicates some false positives, expected with SMOTE + threshold tuning.
    - Balanced results in C show robust performance across balanced data.
    """)
