import streamlit as st

def about_page():
   st.title("ℹ️ About the Model")

   st.markdown("""
   ## Model Overview
   This fraud detection model is built as a **custom pipeline** that includes:
   
   """)
   
   st.image("Images/Model_Architecture/image.png", caption="Model Architecture", width=600)
   
   st.markdown("""

   1. **Feature Engineering**  
      - Interaction: `Category x PaymentMethod`  
      - Ratio: `paymentMethodAgeDays / accountAgeDays`  
      - Binning: `accountAgeDays` into new/medium/old  
      - Time Feature: Categorizing `localTime` into time-of-day segments  

   2. **Preprocessing**
      - Imputation for missing values (median for numeric, mode for categorical)  
      - One-hot encoding for categorical variables  
      - Log transformation for skewed numerical features  
      - Scaling (StandardScaler for skewed features, MinMaxScaler for symmetric features)  

   3. **Resampling**
      - **SMOTE** used to handle class imbalance (fraudulent cases are rare).  

   4. **Model Training**
      - Trained using **Logistic Regression** (can be switched to RandomForest/XGBoost).  
      - Evaluated on multiple hold-out datasets for robustness.

   ---
   
   ## Threshold Tuning
   To improve recall and precision trade-off for fraud detection:
   - **Optimal Threshold (found during training):** `0.8370`  
   - At this threshold:  
   - **Precision = 0.955**  
   - **Recall = 0.991**

   This threshold ensures that the **model catches nearly all fraud cases** (high recall) while keeping **false positives low** (high precision).

   ---
   """)
   
   st.image("Images/MLOps_Architecture/image.png", caption="MLOps Architecture", width=600)
   
   st.markdown("""
   ## MLOps Implementation
   This repository demonstrates **end-to-end MLOps practices**:
   - **Experiment Tracking:** All training experiments tracked using **MLflow** (parameters, metrics, artifacts).  
   - **Containerization:** Model and FastAPI inference service **Dockerized** for portability.  
   - **Kubernetes Deployment:** FastAPI container deployed on **Minikube** for scalable local testing.  
   - **Monitoring:** Integrated **Prometheus + Grafana** to monitor API metrics (latency, request count, status codes).  

   This setup ensures **reproducibility, scalability, and observability** of the entire ML lifecycle.

   ---

   ## Key Features Used
   - `accountAgeDays`
   - `numItems`
   - `localTime`
   - `paymentMethod`
   - `paymentMethodAgeDays`
   - `isWeekend`
   - `Category`

   ---

   ### Prediction Classes
   - **0 → Legitimate Transaction**
   - **1 → Fraudulent Transaction**

   ---
   """)
