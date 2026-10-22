import mlflow
from mlflow.tracking import MlflowClient
import subprocess

# 1. Model details
model_name = "FraudDetectionPipeline"
model_version = "4"   # or use alias e.g. "champion"

# 2. Get model version info (resolves to run and artifact path)
client = MlflowClient()
mv = client.get_model_version(model_name, model_version)
source_uri = mv.source  # This will be something like: runs:/<run_id>/fraud_pipeline.pkl

print(f"Resolved model source URI: {source_uri}")

# 3. Build Docker image from the run URI
#    (This calls CLI from Python)
image_name = "fraud-detection-image"

subprocess.run([
    "mlflow", "models", "build-docker",
    "--model-uri", source_uri,
    "--name", image_name
], check=True)

print(f"Docker image '{image_name}' built successfully!")
print(f"Run with: docker run -p 5001:8080 {image_name}")
