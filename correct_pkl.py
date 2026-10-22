# fix_pickle_path.py

import joblib
import os
from Src.model import FraudPipeline, FeatureEngineering, LogTransformer, Preprocessing  # ✅ Needed to ensure correct module resolution

# Path to old notebook-pickled model
old_model_path = os.path.join("Notebooks", "artifacts", "fraud_pipeline_old.pkl")

# Path to save corrected model
new_model_path = os.path.join("Notebooks", "artifacts", "fraud_pipeline.pkl")

# Load old notebook-pickled model
model = joblib.load(old_model_path)

# Re-save model with correct class reference
joblib.dump(model, new_model_path)

print(f"✅ Model re-saved to: {new_model_path}")
